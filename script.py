import asyncio
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from image_search import (
    ImageSearchMatch,
    OcrUnavailableError,
    RapidOcrRecognizer,
    search_image_candidates,
)
from request_limits import (
    ImageRejectedError,
    SlidingWindowRateLimiter,
    validate_image_file,
)
from turbo_database import (
    DEFAULT_RESULT_LIMIT,
    MIN_PARTIAL_SEARCH_LENGTH,
    SearchResult,
    TurboDatabase,
    ensure_sqlite_database,
    normalize_number,
)
from vin_search import (
    NhtsaVinDecoder,
    VinDecoderError,
    VinRecord,
    VinStore,
    extract_vin,
    format_online_vin,
    format_pending_vin,
    format_verified_vin,
)
from vin_unresolved import UnresolvedVinStore
from vin_online_search import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_YANDEX_MODEL,
    GeminiVinSearcher,
    VinOnlineSearchError,
    VinOnlineSearcherRouter,
    YandexVinSearcher,
    attach_catalog_articles,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
API_TOKEN = os.environ.get("API_TOKEN")
DEFAULT_DATABASE_DOWNLOAD_URL = (
    "https://media.githubusercontent.com/media/"
    "Wraithyavich/database_bot/master/turbo_parts.sqlite"
)
MAX_IMAGE_FILE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
UPDATE_QUEUE_SIZE = 64
MAX_CONCURRENT_UPDATES = 4
OCR_ACQUIRE_TIMEOUT = 0.05
ALLOWED_IMAGE_SUFFIXES = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
VIN_SEED_PATH = BASE_DIR / "vin_verified.json"


def resolve_database_path() -> Path:
    configured_path = os.environ.get("DATABASE_PATH")
    path = Path(configured_path) if configured_path else BASE_DIR / "turbo_search.sqlite"
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def resolve_vin_database_path() -> Path:
    configured_path = os.environ.get("VIN_DATABASE_PATH")
    path = Path(configured_path) if configured_path else BASE_DIR / "vin_cache.sqlite"
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def resolve_unresolved_vin_database_path() -> Path:
    configured_path = os.environ.get("VIN_UNRESOLVED_DATABASE_PATH")
    path = (
        Path(configured_path)
        if configured_path
        else BASE_DIR / "vin_unresolved.sqlite"
    )
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def parse_allowed_user_ids(value: str | None) -> frozenset[int]:
    if not value:
        return frozenset()

    allowed: set[int] = set()
    for token in value.replace(",", " ").replace(";", " ").split():
        try:
            user_id = int(token)
        except ValueError as error:
            raise ValueError(
                "VIN_ALLOWED_USER_IDS must contain Telegram numeric IDs"
            ) from error
        if user_id <= 0:
            raise ValueError(
                "VIN_ALLOWED_USER_IDS must contain positive Telegram IDs"
            )
        allowed.add(user_id)
    return frozenset(allowed)


def parse_optional_user_id(value: str | None) -> int | None:
    parsed = parse_allowed_user_ids(value)
    if not parsed:
        return None
    if len(parsed) != 1:
        raise ValueError(
            "DAILY_GREETING_USER_ID must contain exactly one Telegram ID"
        )
    return next(iter(parsed))


DATABASE = TurboDatabase(resolve_database_path())
OCR_RECOGNIZER = RapidOcrRecognizer()
VIN_STORE = VinStore(resolve_vin_database_path())
VIN_UNRESOLVED_STORE = UnresolvedVinStore(
    resolve_unresolved_vin_database_path()
)
VIN_DECODER = NhtsaVinDecoder()
YANDEX_VIN_SEARCHER = YandexVinSearcher(
    os.environ.get("YANDEX_API_KEY"),
    os.environ.get("YANDEX_FOLDER_ID"),
    search_type=os.environ.get(
        "YANDEX_SEARCH_TYPE",
        "SEARCH_TYPE_RU",
    ),
    model=os.environ.get("YANDEX_MODEL", DEFAULT_YANDEX_MODEL),
)
GEMINI_VIN_SEARCHER = GeminiVinSearcher(
    os.environ.get("GEMINI_API_KEY"),
    model=os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
)
VIN_ONLINE_SEARCHER = VinOnlineSearcherRouter(
    YANDEX_VIN_SEARCHER,
    GEMINI_VIN_SEARCHER,
)
VIN_ALLOWED_USER_IDS = parse_allowed_user_ids(
    os.environ.get("VIN_ALLOWED_USER_IDS")
)
DAILY_GREETING_USER_ID = parse_optional_user_id(
    os.environ.get("DAILY_GREETING_USER_ID")
    or os.environ.get("VIN_DAILY_GREETING_USER_ID")
)
DAILY_GREETING_TEXT = (
    os.environ.get("DAILY_GREETING_TEXT")
    or os.environ.get("VIN_DAILY_GREETING_TEXT")
    or ""
).strip()
VIN_SEARCH_READY = False
VIN_UNRESOLVED_READY = False
TELEGRAM_MESSAGE_LIMIT = 4000
OCR_SEMAPHORE = asyncio.Semaphore(1)
VIN_ONLINE_SEMAPHORE = asyncio.Semaphore(1)
MOSCOW_TIMEZONE = timezone(timedelta(hours=3))
DAILY_GREETING_EVENT_KEY = "configured_vin_daily_greeting"
USER_IMAGE_RATE_LIMITER = SlidingWindowRateLimiter(
    limit=3,
    window_seconds=60,
)
GLOBAL_IMAGE_RATE_LIMITER = SlidingWindowRateLimiter(
    limit=12,
    window_seconds=60,
    max_keys=1,
)
RATE_LIMIT_NOTICE_LIMITER = SlidingWindowRateLimiter(
    limit=1,
    window_seconds=30,
)
USER_TEXT_RATE_LIMITER = SlidingWindowRateLimiter(
    limit=30,
    window_seconds=60,
)
GLOBAL_TEXT_RATE_LIMITER = SlidingWindowRateLimiter(
    limit=180,
    window_seconds=60,
    max_keys=1,
)


def clean_text(value: str) -> str:
    value = value.strip().replace("\r", "").replace("\n", "").replace("\ufeff", "")
    return " ".join(value.split())


def format_search_result(result: SearchResult) -> list[str]:
    query = clean_text(result.original_query)
    if not result.normalized_query:
        return ["❌ Введите номер или артикул, содержащий буквы или цифры."]

    if not result.matches:
        return [f"❌ Ничего не найдено по запросу {query}."]

    if result.fallback_used:
        heading = (
            f"🔎 По запросу {query} ничего не найдено. "
            f"Использован вариант {result.matched_query}:"
        )
    elif result.exact:
        heading = f"🔎 Найденные артикулы для {query}:"
    else:
        heading = f"🔎 Совпадения по части номера {query}:"

    lines = [heading]
    for match in result.matches:
        meaningful_categories = [
            category for category in match.categories if category != "Прочее"
        ]
        category_suffix = (
            f" — {', '.join(meaningful_categories)}"
            if meaningful_categories
            else ""
        )
        lines.append(f"• {match.article}{category_suffix}")

    if result.truncated:
        lines.extend(
            [
                "",
                (
                    f"Показаны первые {DEFAULT_RESULT_LIMIT} результатов. "
                    "Введите более точный номер."
                ),
            ]
        )
    return lines


def format_image_search_results(
    matches: tuple[ImageSearchMatch, ...],
) -> list[str]:
    if not matches:
        return [
            "❌ На изображении не найден номер из базы.",
            "Сфотографируйте шильдик крупнее, без бликов и размытия.",
        ]

    lines = ["📷 Распознанные номера:"]
    seen_numbers: set[tuple[str, str]] = set()
    articles: dict[str, set[str]] = {}
    truncated = False

    for image_match in matches:
        recognized = image_match.recognized_value
        searched = image_match.searched_value
        number_key = (normalize_number(recognized), normalize_number(searched))
        if number_key not in seen_numbers:
            seen_numbers.add(number_key)
            if normalize_number(recognized) == normalize_number(searched):
                lines.append(f"• {recognized}")
            else:
                lines.append(f"• {recognized} → {searched}")

        for article_match in image_match.result.matches:
            meaningful_categories = {
                category
                for category in article_match.categories
                if category != "Прочее"
            }
            articles.setdefault(article_match.article, set()).update(
                meaningful_categories
            )
        truncated = truncated or image_match.result.truncated

    lines.extend(["", "🔎 Найденные артикулы:"])
    for article, categories in articles.items():
        category_suffix = (
            f" — {', '.join(sorted(categories))}" if categories else ""
        )
        lines.append(f"• {article}{category_suffix}")

    if truncated:
        lines.append(f"Показаны первые {DEFAULT_RESULT_LIMIT} результатов.")
    return lines


def split_long_message(
    lines: list[str], *, limit: int = TELEGRAM_MESSAGE_LIMIT
) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for original_line in lines:
        line = original_line
        if len(line) > limit:
            line = f"{line[: limit - 1]}…"

        added_length = len(line) + (1 if current else 0)
        if current and current_length + added_length > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_length = len(line)
        else:
            current.append(line)
            current_length += added_length

    if current:
        chunks.append("\n".join(current))
    return chunks


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    emoji_id = "5247029251940586192"
    welcome_text = (
        f'<tg-emoji emoji-id="{emoji_id}">😊</tg-emoji> '
        "ТУРБОНАЙЗЕР бот приветствует!\n"
        "Введите E&E P/N, Turbo P/N, OEM, JRONE или FLP номер.\n\n"
        "Пример: 17201-52010 или 1000-010-006\n\n"
        "Можно отправить 17-значный VIN: бот проверит базу и при необходимости "
        "выполнит предварительный поиск в интернете.\n\n"
        "Также можно отправить фотографию шильдика или номера.\n\n"
        f"Можно искать по части номера — минимум "
        f"{MIN_PARTIAL_SEARCH_LENGTH} символа. "
        "Дефисы, пробелы и регистр не имеют значения."
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")


async def handle_vin_query(
    update: Update,
    vin: str,
) -> None:
    message = update.message
    if message is None:
        return

    if not VIN_SEARCH_READY:
        await message.reply_text(
            "❌ Поиск по VIN временно недоступен. Попробуйте позднее."
        )
        return

    try:
        record = await asyncio.to_thread(VIN_STORE.lookup, vin)
        if record is not None and record.status == "verified":
            await update_unresolved_vin(record)
            lines = format_verified_vin(record)
        else:
            decoder_failed = False
            online_search_note = ""
            failure_code = ""
            if record is None:
                try:
                    record = await asyncio.to_thread(VIN_DECODER.decode, vin)
                except VinDecoderError:
                    logger.warning(
                        "Базовый VIN-декодер недоступен для VIN …%s",
                        vin[-6:],
                        exc_info=True,
                    )
                    decoder_failed = True
                    failure_code = "vin_decoder_error"
                    record = VinRecord(vin=vin, status="pending")

            if not record.online_search_at:
                if not VIN_ONLINE_SEARCHER.enabled:
                    failure_code = "online_search_not_configured"
                    online_search_note = (
                        "Онлайн-поиск пока не настроен администратором."
                    )
                else:
                    async with VIN_ONLINE_SEMAPHORE:
                        latest = await asyncio.to_thread(
                            VIN_STORE.lookup,
                            vin,
                        )
                        if latest is not None and (
                            latest.status == "verified"
                            or latest.online_search_at
                        ):
                            record = latest
                        else:
                            try:
                                record = await asyncio.to_thread(
                                    VIN_ONLINE_SEARCHER.search,
                                    record,
                                )
                                record = await asyncio.to_thread(
                                    attach_catalog_articles,
                                    record,
                                    DATABASE,
                                )
                            except VinOnlineSearchError:
                                failure_code = "online_search_error"
                                logger.warning(
                                    "Онлайн-поиск не выполнен для VIN …%s",
                                    vin[-6:],
                                    exc_info=True,
                                )
                                online_search_note = (
                                    "Онлайн-поиск временно недоступен."
                                )

            record = await asyncio.to_thread(
                VIN_STORE.record_request,
                vin,
                decoded=record,
            )
            if record.status == "verified":
                await update_unresolved_vin(record)
                lines = format_verified_vin(record)
            elif record.online_search_at:
                if record.fitments:
                    await update_unresolved_vin(record)
                else:
                    failure_code = "no_supported_turbo_numbers"
                    online_search_note = (
                        "В открытых источниках не найдено достаточно "
                        "обоснованных номеров турбин."
                    )
                    await update_unresolved_vin(
                        record,
                        failure_code=failure_code,
                        failure_detail=online_search_note,
                    )
                lines = format_online_vin(record)
            else:
                await update_unresolved_vin(
                    record,
                    failure_code=failure_code or "vin_not_processed",
                    failure_detail=online_search_note,
                )
                lines = format_pending_vin(
                    record,
                    decoder_failed=decoder_failed,
                )
                if online_search_note:
                    lines.extend(["", online_search_note])
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        logger.exception("Ошибка VIN-поиска для VIN …%s", vin[-6:])
        await update_unresolved_vin(
            VinRecord(vin=vin, status="pending"),
            failure_code="vin_pipeline_error",
            failure_detail="Внутренняя ошибка обработки VIN.",
        )
        await message.reply_text(
            "❌ VIN-база временно недоступна. Попробуйте позднее."
        )
        return

    for chunk in split_long_message(lines):
        await message.reply_text(chunk)


async def update_unresolved_vin(
    record: VinRecord,
    *,
    failure_code: str = "",
    failure_detail: str = "",
) -> None:
    if not VIN_UNRESOLVED_READY:
        return

    try:
        if failure_code:
            await asyncio.to_thread(
                VIN_UNRESOLVED_STORE.record_failure,
                record.vin,
                failure_code=failure_code,
                failure_detail=failure_detail,
                record=record,
            )
        else:
            await asyncio.to_thread(VIN_UNRESOLVED_STORE.remove, record.vin)
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        logger.exception(
            "Не удалось обновить отдельную базу неразобранных VIN …%s",
            record.vin[-6:],
        )


def current_moscow_date() -> str:
    return datetime.now(MOSCOW_TIMEZONE).date().isoformat()


async def claim_daily_greeting(*, user_id: int) -> bool:
    if (
        DAILY_GREETING_USER_ID is None
        or not DAILY_GREETING_TEXT
        or user_id != DAILY_GREETING_USER_ID
    ):
        return False

    try:
        return await asyncio.to_thread(
            VIN_STORE.claim_daily_event,
            DAILY_GREETING_EVENT_KEY,
            current_moscow_date(),
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        logger.exception(
            "Не удалось сохранить отметку ежедневного сообщения"
        )
        return False


async def send_daily_greeting(update: Update, *, claimed: bool) -> None:
    message = update.message
    if not claimed or message is None:
        return
    try:
        await message.reply_text(DAILY_GREETING_TEXT)
    except TelegramError:
        logger.warning(
            "Не удалось отправить ежедневное сообщение",
            exc_info=True,
        )


async def _handle_message_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None or update.message.text is None:
        return

    user_input = clean_text(update.message.text)
    if not user_input:
        return

    user = update.effective_user
    rate_limit_key = user.id if user is not None else update.message.chat_id
    vin = extract_vin(user_input)
    if vin is not None:
        if user is None or user.id not in VIN_ALLOWED_USER_IDS:
            notice = RATE_LIMIT_NOTICE_LIMITER.allow(rate_limit_key)
            if notice.allowed:
                await update.message.reply_text(
                    "🔒 Поиск по VIN доступен только допущенным пользователям."
                )
            return
        await handle_vin_query(update, vin)
        return

    user_limit = USER_TEXT_RATE_LIMITER.allow(rate_limit_key)
    if not user_limit.allowed:
        notice = RATE_LIMIT_NOTICE_LIMITER.allow(rate_limit_key)
        if notice.allowed:
            await update.message.reply_text(
                "⏳ Слишком много запросов. Повторите немного позже."
            )
        return

    global_limit = GLOBAL_TEXT_RATE_LIMITER.allow("global")
    if not global_limit.allowed:
        notice = RATE_LIMIT_NOTICE_LIMITER.allow(rate_limit_key)
        if notice.allowed:
            await update.message.reply_text(
                "⏳ Бот временно перегружен. Повторите запрос немного позже."
            )
        return

    try:
        result = await asyncio.to_thread(DATABASE.search, user_input)
    except (OSError, sqlite3.Error, RuntimeError):
        logger.exception("Ошибка поиска в SQLite")
        await update.message.reply_text(
            "❌ База данных временно недоступна. Попробуйте ещё раз позже."
        )
        return

    for chunk in split_long_message(format_search_result(result)):
        await update.message.reply_text(chunk)


async def handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    claimed = (
        await claim_daily_greeting(user_id=user.id)
        if user is not None
        else False
    )
    try:
        await _handle_message_request(update, context)
    finally:
        await send_daily_greeting(update, claimed=claimed)


async def _handle_image_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None:
        return

    if message.photo:
        image_file = message.photo[-1]
        file_id = image_file.file_id
        file_size = image_file.file_size or 0
        suffix = ".jpg"
    elif message.document is not None:
        file_id = message.document.file_id
        file_size = message.document.file_size or 0
        suffix = Path(message.document.file_name or "image.jpg").suffix or ".jpg"
    else:
        return

    if suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
        suffix = ".img"

    if file_size > MAX_IMAGE_FILE_BYTES:
        await message.reply_text(
            "❌ Изображение слишком большое. Максимальный размер — 8 МБ."
        )
        return

    user = update.effective_user
    rate_limit_key = user.id if user is not None else message.chat_id
    user_limit = USER_IMAGE_RATE_LIMITER.allow(rate_limit_key)
    if not user_limit.allowed:
        notice = RATE_LIMIT_NOTICE_LIMITER.allow(rate_limit_key)
        if notice.allowed:
            await message.reply_text(
                "⏳ Слишком много запросов по фото. "
                f"Повторите примерно через {user_limit.retry_after} сек."
            )
        return

    global_limit = GLOBAL_IMAGE_RATE_LIMITER.allow("global")
    if not global_limit.allowed:
        notice = RATE_LIMIT_NOTICE_LIMITER.allow(rate_limit_key)
        if notice.allowed:
            await message.reply_text(
                "⏳ Бот временно перегружен обработкой фотографий. "
                f"Повторите примерно через {global_limit.retry_after} сек."
            )
        return

    try:
        await asyncio.wait_for(
            OCR_SEMAPHORE.acquire(),
            timeout=OCR_ACQUIRE_TIMEOUT,
        )
    except TimeoutError:
        notice = RATE_LIMIT_NOTICE_LIMITER.allow(rate_limit_key)
        if notice.allowed:
            await message.reply_text(
                "⏳ Сейчас обрабатывается другое изображение. "
                "Повторите попытку через несколько секунд."
            )
        return

    try:
        telegram_file = await context.bot.get_file(file_id)
        with tempfile.TemporaryDirectory(prefix="turbo_ocr_") as temp_dir:
            image_path = Path(temp_dir) / f"image{suffix.lower()}"
            await telegram_file.download_to_drive(custom_path=image_path)
            await asyncio.to_thread(
                validate_image_file,
                image_path,
                max_bytes=MAX_IMAGE_FILE_BYTES,
                max_pixels=MAX_IMAGE_PIXELS,
            )
            candidates = await asyncio.to_thread(
                OCR_RECOGNIZER.recognize, image_path
            )
            matches = await asyncio.to_thread(
                search_image_candidates, DATABASE, candidates
            )
    except OcrUnavailableError:
        logger.exception("OCR-компонент не удалось загрузить")
        await message.reply_text(
            "❌ Поиск по изображению временно недоступен: "
            "OCR-компонент не запустился."
        )
        return
    except ImageRejectedError as error:
        logger.info("Изображение отклонено: %s", error)
        await message.reply_text(
            "❌ Файл слишком большой, повреждён или имеет чрезмерное разрешение. "
            "Отправьте изображение до 8 МБ и 16 мегапикселей."
        )
        return
    except (OSError, RuntimeError, sqlite3.Error, TelegramError):
        logger.exception("Ошибка обработки изображения")
        await message.reply_text(
            "❌ Не удалось обработать изображение. "
            "Попробуйте другое фото без бликов и размытия."
        )
        return
    finally:
        OCR_SEMAPHORE.release()

    if not candidates:
        lines = [
            "❌ Не удалось распознать номер на изображении.",
            "Сфотографируйте шильдик крупнее, без бликов и размытия.",
        ]
    else:
        lines = format_image_search_results(matches)

    for chunk in split_long_message(lines):
        await message.reply_text(chunk)


async def handle_image(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    claimed = (
        await claim_daily_greeting(user_id=user.id)
        if user is not None
        else False
    )
    try:
        await _handle_image_request(update, context)
    finally:
        await send_daily_greeting(update, claimed=claimed)


def main() -> None:
    global DATABASE, VIN_SEARCH_READY, VIN_UNRESOLVED_READY

    if not API_TOKEN:
        raise ValueError("❌ Переменная окружения API_TOKEN не задана!")

    database_path = ensure_sqlite_database(
        DATABASE.path,
        download_url=os.environ.get(
            "DATABASE_DOWNLOAD_URL", DEFAULT_DATABASE_DOWNLOAD_URL
        ),
        cache_dir=os.environ.get("DATABASE_CACHE_DIR"),
    )
    if database_path != DATABASE.path:
        logger.warning(
            "В репозитории обнаружен Git LFS-указатель. "
            "SQLite загружена в локальный кеш: %s",
            database_path,
        )
        DATABASE = TurboDatabase(database_path)

    try:
        OCR_RECOGNIZER.check_available()
    except OcrUnavailableError:
        logger.exception(
            "OCR не запустился. Текстовый поиск продолжит работать, "
            "но поиск по изображению будет недоступен."
        )
    else:
        logger.info("OCR успешно загружен")

    stats = DATABASE.validate()
    logger.info(
        "SQLite загружена: %s артикулов, %s номеров, %s кроссов, %s источников",
        stats.parts,
        stats.numbers,
        stats.crossrefs,
        stats.sources,
    )

    try:
        vin_stats = VIN_STORE.initialize(seed_path=VIN_SEED_PATH)
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        logger.exception(
            "VIN-хранилище не запустилось. Остальные виды поиска продолжат работать."
        )
    else:
        VIN_SEARCH_READY = True
        logger.info(
            "VIN-хранилище загружено: %s проверенных, %s ожидают проверки, "
            "%s запросов",
            vin_stats.verified,
            vin_stats.pending,
            vin_stats.requests,
        )
        if VIN_ONLINE_SEARCHER.enabled:
            logger.info(
                "Онлайн-поиск VIN включён: %s",
                VIN_ONLINE_SEARCHER.description,
            )
        else:
            logger.warning(
                "Онлайн-поиск VIN выключен: настройте Yandex Search API "
                "или Gemini API"
            )
        if YANDEX_VIN_SEARCHER.api_key and not YANDEX_VIN_SEARCHER.folder_id:
            logger.warning(
                "Yandex Search API не активирован: YANDEX_FOLDER_ID не задан"
            )

    try:
        unresolved_stats = VIN_UNRESOLVED_STORE.initialize()
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        logger.exception(
            "Отдельная база неразобранных VIN не запустилась. "
            "Остальные виды поиска продолжат работать."
        )
    else:
        VIN_UNRESOLVED_READY = True
        logger.info(
            "База неразобранных VIN загружена: %s VIN, %s запросов",
            unresolved_stats.unique_vins,
            unresolved_stats.requests,
        )

    if VIN_ALLOWED_USER_IDS:
        logger.info(
            "Белый список VIN загружен: %s пользователей",
            len(VIN_ALLOWED_USER_IDS),
        )
    else:
        logger.warning(
            "Белый список VIN пуст: VIN-поиск недоступен пользователям"
        )

    if DAILY_GREETING_USER_ID is not None and DAILY_GREETING_TEXT:
        logger.info(
            "Персональное ежедневное сообщение после поиска включено"
        )

    update_queue: asyncio.Queue[object] = asyncio.Queue(maxsize=UPDATE_QUEUE_SIZE)
    app = (
        Application.builder()
        .token(API_TOKEN)
        .update_queue(update_queue)
        .concurrent_updates(MAX_CONCURRENT_UPDATES)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("ТУРБОНАЙЗЕР запущен. Основной источник: %s", DATABASE.path)
    app.run_polling()


if __name__ == "__main__":
    main()
