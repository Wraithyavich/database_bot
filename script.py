import asyncio
import html
import json
import logging
import os
import sqlite3
import tempfile
import urllib.error
import urllib.request
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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
    ReverseSearchResult,
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
    format_manual_vin,
    format_online_vin,
    format_verified_vin,
)
from vin_admin_reply import (
    VinAdminReplyError,
    confirm_admin_vin_record,
    format_admin_notification,
    is_admin_confirmation,
    is_admin_reply_candidate,
    parse_admin_vin_reply,
)
from vin_unresolved import UnresolvedVinStore
from vin_online_search import (
    DEFAULT_YANDEX_MODEL,
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
VIN_MANUAL_CALLBACK_PREFIX = "vin_review:"
VIN_MANUAL_BUTTON_TEXT = "Запросить проверку"


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


def parse_allowed_user_ids(
    value: str | None,
    *,
    variable_name: str = "VIN_ALLOWED_USER_IDS",
) -> frozenset[int]:
    if not value:
        return frozenset()

    allowed: set[int] = set()
    for token in value.replace(",", " ").replace(";", " ").split():
        try:
            user_id = int(token)
        except ValueError as error:
            raise ValueError(
                f"{variable_name} must contain Telegram numeric IDs"
            ) from error
        if user_id <= 0:
            raise ValueError(
                f"{variable_name} must contain positive Telegram IDs"
            )
        allowed.add(user_id)
    return frozenset(allowed)


def parse_optional_user_id(value: str | None) -> int | None:
    parsed = parse_allowed_user_ids(
        value,
        variable_name="DAILY_GREETING_USER_ID",
    )
    if not parsed:
        return None
    if len(parsed) != 1:
        raise ValueError(
            "DAILY_GREETING_USER_ID must contain exactly one Telegram ID"
        )
    return next(iter(parsed))


def parse_boolean(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("Boolean setting must be true or false")


def parse_bounded_integer(
    value: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
    variable_name: str,
) -> int:
    try:
        parsed = default if value is None or not value.strip() else int(value)
    except ValueError as error:
        raise ValueError(f"{variable_name} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(
            f"{variable_name} must be between {minimum} and {maximum}"
        )
    return parsed


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
VIN_ONLINE_SEARCHER = VinOnlineSearcherRouter(
    YANDEX_VIN_SEARCHER,
)
VIN_ALLOWED_USER_IDS = parse_allowed_user_ids(
    os.environ.get("VIN_ALLOWED_USER_IDS")
)
VIN_ADMIN_USER_IDS = parse_allowed_user_ids(
    os.environ.get("VIN_ADMIN_USER_IDS"),
    variable_name="VIN_ADMIN_USER_IDS",
)
VIN_OBSERVER_ENABLED = parse_boolean(
    os.environ.get("VIN_OBSERVER_ENABLED"),
)
VIN_OBSERVER_INTERVAL_SECONDS = parse_bounded_integer(
    os.environ.get("VIN_OBSERVER_INTERVAL_SECONDS"),
    default=900,
    minimum=60,
    maximum=86_400,
    variable_name="VIN_OBSERVER_INTERVAL_SECONDS",
)
VIN_OBSERVER_INITIAL_DELAY_SECONDS = parse_bounded_integer(
    os.environ.get("VIN_OBSERVER_INITIAL_DELAY_SECONDS"),
    default=60,
    minimum=5,
    maximum=3_600,
    variable_name="VIN_OBSERVER_INITIAL_DELAY_SECONDS",
)
VIN_OBSERVER_DAILY_LIMIT = parse_bounded_integer(
    os.environ.get("VIN_OBSERVER_DAILY_LIMIT"),
    default=5,
    minimum=1,
    maximum=50,
    variable_name="VIN_OBSERVER_DAILY_LIMIT",
)
VIN_OBSERVER_RETRY_SECONDS = parse_bounded_integer(
    os.environ.get("VIN_OBSERVER_RETRY_SECONDS"),
    default=86_400,
    minimum=3_600,
    maximum=604_800,
    variable_name="VIN_OBSERVER_RETRY_SECONDS",
)
VIN_AGENT_ENABLED = parse_boolean(
    os.environ.get("VIN_AGENT_ENABLED"),
)
VIN_AGENT_TRIGGER_URL = os.environ.get(
    "VIN_AGENT_TRIGGER_URL",
    "",
).strip()
VIN_AGENT_TRIGGER_TOKEN = os.environ.get(
    "VIN_AGENT_TRIGGER_TOKEN",
    "",
).strip()
if VIN_AGENT_ENABLED and (
    not VIN_AGENT_TRIGGER_URL or not VIN_AGENT_TRIGGER_TOKEN
):
    raise ValueError(
        "VIN_AGENT_TRIGGER_URL and VIN_AGENT_TRIGGER_TOKEN are required "
        "when VIN_AGENT_ENABLED=true"
    )
if VIN_AGENT_ENABLED and VIN_OBSERVER_ENABLED:
    raise ValueError(
        "VIN_AGENT_ENABLED and VIN_OBSERVER_ENABLED cannot be enabled together"
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
VIN_OBSERVER_MAX_RETRY_SECONDS = 604_800
VIN_OBSERVER_TASK: asyncio.Task[None] | None = None
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


def telegram_code(value: str) -> str:
    return f"<code>{html.escape(value, quote=False)}</code>"


def format_search_result(result: SearchResult) -> list[str]:
    query = html.escape(clean_text(result.original_query), quote=False)
    if not result.normalized_query:
        return ["❌ Введите номер или артикул, содержащий буквы или цифры."]

    if not result.matches:
        return [f"❌ Ничего не найдено по запросу {query}."]

    if result.fallback_used:
        heading = (
            f"🔎 По запросу {query} ничего не найдено. "
            f"Использован вариант "
            f"{html.escape(result.matched_query, quote=False)}:"
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
            " — "
            + ", ".join(
                html.escape(category, quote=False)
                for category in meaningful_categories
            )
            if meaningful_categories
            else ""
        )
        lines.append(f"• {telegram_code(match.article)}{category_suffix}")

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


def format_reverse_search_result(result: ReverseSearchResult) -> list[str]:
    if not result.found:
        lines = ["🔎 Найдено несколько возможных артикулов. Уточните запрос:"]
        lines.extend(f"• {telegram_code(article)}" for article in result.candidates)
        return lines

    assert result.matched_article is not None
    requested = clean_text(result.original_query)
    if result.resolution == "exact":
        lines = [f"🔎 Артикул: {telegram_code(result.matched_article)}"]
    else:
        lines = [
            f"🔎 Запрос: {telegram_code(requested)}",
            f"Найденный артикул: {telegram_code(result.matched_article)}",
        ]

    meaningful_categories = tuple(
        category for category in result.categories if category != "Прочее"
    )
    if meaningful_categories:
        lines.append(
            "Тип: "
            + ", ".join(
                html.escape(category, quote=False)
                for category in meaningful_categories
            )
        )

    groups = (
        ("turbo_pn", "Turbo P/N"),
        ("vehicle_oem", "OEM / Vehicle OE"),
        ("component_pn", "OEM/P/N детали"),
    )
    visible_count = 0
    for kind, heading in groups:
        numbers = result.numbers_of_kind(kind)
        if not numbers:
            continue
        visible_count += len(numbers)
        lines.extend(["", f"{heading} — {len(numbers)}:"])
        lines.extend(telegram_code(number.number) for number in numbers)

    if not result.numbers_of_kind("turbo_pn") and not result.numbers_of_kind(
        "vehicle_oem"
    ):
        lines.extend(
            [
                "",
                (
                    "Артикул найден, но в текущей базе для него не указаны "
                    "Turbo P/N или OEM-номера."
                ),
            ]
        )
    elif visible_count == 0:
        lines.append(
            "Артикул найден, но в текущей базе нет достоверных обратных связей."
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
                lines.append(f"• {html.escape(recognized, quote=False)}")
            else:
                lines.append(
                    f"• {html.escape(recognized, quote=False)} → "
                    f"{html.escape(searched, quote=False)}"
                )

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
            " — "
            + ", ".join(
                html.escape(category, quote=False)
                for category in sorted(categories)
            )
            if categories
            else ""
        )
        lines.append(f"• {telegram_code(article)}{category_suffix}")

    if truncated:
        lines.append(f"Показаны первые {DEFAULT_RESULT_LIMIT} результатов.")
    return lines


def split_long_message(
    lines: list[str],
    *,
    limit: int = TELEGRAM_MESSAGE_LIMIT,
    number_parts: bool = False,
) -> list[str]:
    if limit < 16:
        raise ValueError("limit должен быть не меньше 16")

    content_limit = limit - 16 if number_parts else limit
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0

    for original_line in lines:
        line = original_line
        if len(line) > content_limit:
            line = f"{line[: content_limit - 1]}…"

        added_length = len(line) + (1 if current else 0)
        if current and current_length + added_length > content_limit:
            chunks.append("\n".join(current))
            current = [line]
            current_length = len(line)
        else:
            current.append(line)
            current_length += added_length

    if current:
        chunks.append("\n".join(current))
    if number_parts and len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{index}/{total}\n{chunk}" for index, chunk in enumerate(chunks, 1)]
    return chunks


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    emoji_id = "5247029251940586192"
    welcome_text = (
        f'<tg-emoji emoji-id="{emoji_id}">😊</tg-emoji> '
        "ТУРБОНАЙЗЕР бот приветствует!\n"
        "Отправьте Turbo P/N, OEM-номер или артикул E&E Turbo.\n\n"
        "Прямой поиск: 17201-52010\n"
        "Обратный поиск: Turbo-G189\n\n"
        "Также поддерживаются JRONE и FLP номера.\n\n"
        "Можно отправить 17-значный VIN: бот проверит базу и при необходимости "
        "выполнит предварительный поиск в интернете.\n\n"
        "Также можно отправить фотографию шильдика или номера.\n\n"
        f"Можно искать по части номера — минимум "
        f"{MIN_PARTIAL_SEARCH_LENGTH} символа. "
        "Дефисы, пробелы и регистр не имеют значения."
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")


def vin_manual_review_markup(vin: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    VIN_MANUAL_BUTTON_TEXT,
                    callback_data=f"{VIN_MANUAL_CALLBACK_PREFIX}{vin}",
                )
            ]
        ]
    )


def format_vin_searching(vin: str) -> str:
    return f"🔎 Ищу информацию по VIN…\n{vin}"


def format_vin_not_found(vin: str) -> str:
    return f"Номера турбины по VIN не найдены.\nVIN: {vin}"


async def send_automatic_vin_result(
    message: object,
    record: VinRecord,
) -> None:
    await message.reply_text(
        "\n".join(format_online_vin(record)),
        parse_mode="HTML",
        reply_markup=vin_manual_review_markup(record.vin),
    )


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
            for chunk in split_long_message(format_verified_vin(record)):
                await message.reply_text(chunk, parse_mode="HTML")
            return

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
                failure_code = "vin_decoder_error"
                record = VinRecord(vin=vin, status="pending")

        if not record.online_search_at:
            if not VIN_ONLINE_SEARCHER.enabled:
                failure_code = "online_search_not_configured"
                online_search_note = "Онлайн-поиск не настроен."
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
            for chunk in split_long_message(format_verified_vin(record)):
                await message.reply_text(chunk, parse_mode="HTML")
            return

        if record.fitments:
            await update_unresolved_vin(record)
            await send_automatic_vin_result(message, record)
            return

        if record.online_search_at:
            failure_code = "no_supported_turbo_numbers"
            online_search_note = "Автоматический поиск пока не дал результата."
        else:
            failure_code = failure_code or "vin_not_processed"

        if not (VIN_AGENT_ENABLED or VIN_OBSERVER_ENABLED):
            await update_unresolved_vin(
                record,
                failure_code=failure_code,
                failure_detail=online_search_note,
            )
            await message.reply_text(
                format_vin_not_found(vin),
                reply_markup=vin_manual_review_markup(vin),
            )
            return

        status_message = await message.reply_text(format_vin_searching(vin))
        user = update.effective_user
        await update_unresolved_vin(
            record,
            failure_code=failure_code,
            failure_detail=online_search_note,
            requester_user_id=user.id if user is not None else None,
            requester_chat_id=message.chat_id,
            requester_username=(
                getattr(user, "username", "") or ""
                if user is not None
                else ""
            ),
            status_message_id=status_message.message_id,
        )
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


async def update_unresolved_vin(
    record: VinRecord,
    *,
    failure_code: str = "",
    failure_detail: str = "",
    requester_user_id: int | None = None,
    requester_chat_id: int | None = None,
    requester_username: str = "",
    status_message_id: int | None = None,
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
                observer_delay_seconds=(
                    0 if VIN_AGENT_ENABLED else VIN_OBSERVER_RETRY_SECONDS
                ),
            )
            if (
                requester_user_id is not None
                and requester_chat_id is not None
                and status_message_id is not None
            ):
                await asyncio.to_thread(
                    VIN_UNRESOLVED_STORE.subscribe_result,
                    record.vin,
                    user_id=requester_user_id,
                    chat_id=requester_chat_id,
                    username=requester_username,
                    status_message_id=status_message_id,
                )
            await trigger_vin_agent(record.vin)
        else:
            await asyncio.to_thread(VIN_UNRESOLVED_STORE.remove, record.vin)
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        logger.exception(
            "Не удалось обновить отдельную базу неразобранных VIN …%s",
            record.vin[-6:],
        )


async def trigger_vin_agent(vin: str) -> None:
    if not VIN_AGENT_ENABLED:
        return

    def send_trigger() -> None:
        request = urllib.request.Request(
            VIN_AGENT_TRIGGER_URL,
            data=json.dumps({"vin": vin}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Observer-Token": VIN_AGENT_TRIGGER_TOKEN,
                "User-Agent": "database-bot/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status != 202:
                raise OSError(
                    f"VIN agent trigger returned HTTP {response.status}"
                )
            response.read(4096)

    try:
        await asyncio.to_thread(send_trigger)
    except (OSError, urllib.error.URLError):
        # The SQLite queue is authoritative. The worker performs a cheap
        # model-free rescue scan, so a lost HTTP wake-up does not lose the VIN.
        logger.warning(
            "Не удалось немедленно разбудить VIN agent для VIN …%s; "
            "запрос остаётся в очереди",
            vin[-6:],
            exc_info=True,
        )


async def notify_manual_review_admins(
    bot: object,
    record: VinRecord,
    *,
    user_id: int,
    username: str = "",
) -> bool:
    notification = format_admin_notification(
        record,
        user_id=user_id,
        username=username,
    )
    delivered = False
    for admin_chat_id in sorted(VIN_ADMIN_USER_IDS):
        try:
            await bot.send_message(
                chat_id=admin_chat_id,
                text=notification,
            )
        except TelegramError:
            logger.warning(
                "Не удалось отправить ручной VIN-запрос …%s",
                record.vin[-6:],
                exc_info=True,
            )
        else:
            delivered = True
    return delivered


async def deliver_subscribed_vin_result(
    bot: object,
    vin: str,
    text: str,
    *,
    with_manual_button: bool = True,
) -> bool:
    subscriptions = await asyncio.to_thread(
        VIN_UNRESOLVED_STORE.pending_result_subscriptions,
        vin,
    )
    if not subscriptions:
        return True
    all_delivered = True
    reply_markup = (
        vin_manual_review_markup(vin)
        if with_manual_button
        else None
    )
    for subscription in subscriptions:
        try:
            await bot.edit_message_text(
                chat_id=subscription.chat_id,
                message_id=subscription.status_message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except TelegramError:
            try:
                await bot.send_message(
                    chat_id=subscription.chat_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            except TelegramError:
                all_delivered = False
                logger.warning(
                    "Не удалось отправить результат VIN …%s пользователю %s",
                    vin[-6:],
                    subscription.user_id,
                    exc_info=True,
                )
                continue
        await asyncio.to_thread(
            VIN_UNRESOLVED_STORE.mark_result_delivered,
            subscription.id,
        )
    return all_delivered


async def handle_vin_manual_review(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    data = query.data or ""
    if not data.startswith(VIN_MANUAL_CALLBACK_PREFIX):
        return
    vin = extract_vin(data.removeprefix(VIN_MANUAL_CALLBACK_PREFIX))
    if vin is None or user.id not in VIN_ALLOWED_USER_IDS:
        await query.answer("Запрос недоступен.", show_alert=True)
        return
    if not VIN_UNRESOLVED_READY or not VIN_ADMIN_USER_IDS:
        await query.answer(
            "Ручная проверка временно недоступна.",
            show_alert=True,
        )
        return

    message = query.message
    chat_id = getattr(message, "chat_id", 0)
    if not chat_id:
        await query.answer("Не удалось определить чат.", show_alert=True)
        return

    try:
        manual_request = await asyncio.to_thread(
            VIN_UNRESOLVED_STORE.claim_manual_request,
            vin,
            user_id=user.id,
            chat_id=chat_id,
            username=getattr(user, "username", "") or "",
        )
        if manual_request is None:
            await query.answer("Запрос уже отправлен.")
            with suppress(TelegramError):
                await query.edit_message_reply_markup(reply_markup=None)
            return

        record = await asyncio.to_thread(VIN_STORE.lookup, vin)
        record = record or VinRecord(vin=vin, status="pending")
        delivered = await notify_manual_review_admins(
            context.bot,
            record,
            user_id=user.id,
            username=getattr(user, "username", "") or "",
        )
        if not delivered:
            await asyncio.to_thread(
                VIN_UNRESOLVED_STORE.release_manual_request,
                manual_request.id,
            )
            await query.answer(
                "Не удалось передать запрос. Попробуйте позже.",
                show_alert=True,
            )
            return
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        logger.exception(
            "Не удалось обработать ручной VIN-запрос …%s",
            vin[-6:],
        )
        await query.answer(
            "Не удалось передать запрос. Попробуйте позже.",
            show_alert=True,
        )
        return

    with suppress(TelegramError):
        await query.edit_message_reply_markup(reply_markup=None)
    await query.answer("Запрос отправлен.")


async def deliver_manual_vin_result(
    bot: object,
    record: VinRecord,
) -> None:
    if not VIN_UNRESOLVED_READY:
        return
    requests = await asyncio.to_thread(
        VIN_UNRESOLVED_STORE.pending_manual_requests,
        record.vin,
    )
    text = "\n".join(format_manual_vin(record))
    for request in requests:
        try:
            await bot.send_message(
                chat_id=request.chat_id,
                text=text,
                parse_mode="HTML",
            )
        except TelegramError:
            logger.warning(
                "Не удалось вернуть ручной результат VIN …%s пользователю %s",
                record.vin[-6:],
                request.user_id,
                exc_info=True,
            )
            continue
        await asyncio.to_thread(
            VIN_UNRESOLVED_STORE.mark_manual_request_completed,
            request.id,
        )


async def run_vin_observer_once(bot: object) -> str:
    if (
        not VIN_OBSERVER_ENABLED
        or not VIN_SEARCH_READY
        or not VIN_UNRESOLVED_READY
        or not VIN_ONLINE_SEARCHER.enabled
    ):
        return "disabled"

    job = await asyncio.to_thread(
        VIN_UNRESOLVED_STORE.claim_due_observer_job,
        daily_limit=VIN_OBSERVER_DAILY_LIMIT,
    )
    if job is None:
        return "idle"

    try:
        record = await asyncio.to_thread(VIN_STORE.lookup, job.vin)
        if record is None:
            record = VinRecord(vin=job.vin, status="pending")
        if record.status == "verified":
            await asyncio.to_thread(VIN_UNRESOLVED_STORE.remove, job.vin)
            return "already_verified"

        if not record.fitments:
            async with VIN_ONLINE_SEMAPHORE:
                latest = await asyncio.to_thread(VIN_STORE.lookup, job.vin)
                if latest is not None and latest.status == "verified":
                    await asyncio.to_thread(
                        VIN_UNRESOLVED_STORE.remove,
                        job.vin,
                    )
                    return "already_verified"
                record = await asyncio.to_thread(
                    VIN_ONLINE_SEARCHER.search,
                    latest or record,
                )
                record = await asyncio.to_thread(
                    attach_catalog_articles,
                    record,
                    DATABASE,
                )
                record = await asyncio.to_thread(
                    VIN_STORE.save_pending,
                    record,
                )

        if record.status == "verified":
            await asyncio.to_thread(VIN_UNRESOLVED_STORE.remove, job.vin)
            return "already_verified"

        if record.fitments:
            delivered = await deliver_subscribed_vin_result(
                bot,
                record.vin,
                "\n".join(format_online_vin(record)),
            )
            if delivered:
                await asyncio.to_thread(
                    VIN_UNRESOLVED_STORE.remove,
                    job.vin,
                )
                logger.info(
                    "VIN-наблюдатель нашёл кандидатов для VIN …%s",
                    job.vin[-6:],
                )
                return "candidates_found"
            await asyncio.to_thread(
                VIN_UNRESOLVED_STORE.complete_observer_attempt,
                job.vin,
                next_delay_seconds=3_600,
                result="candidate_notification_failed",
            )
            return "notification_failed"

        retry_seconds = min(
            VIN_OBSERVER_RETRY_SECONDS
            * (2 ** min(job.attempt_count, 3)),
            VIN_OBSERVER_MAX_RETRY_SECONDS,
        )
        await asyncio.to_thread(
            VIN_UNRESOLVED_STORE.complete_observer_attempt,
            job.vin,
            next_delay_seconds=retry_seconds,
            result="no_supported_turbo_numbers",
        )
        await deliver_subscribed_vin_result(
            bot,
            job.vin,
            format_vin_not_found(job.vin),
        )
        logger.info(
            "VIN-наблюдатель пока не нашёл номера для VIN …%s; "
            "следующая попытка через %s сек.",
            job.vin[-6:],
            retry_seconds,
        )
        return "not_found"
    except VinOnlineSearchError:
        retry_seconds = min(VIN_OBSERVER_RETRY_SECONDS, 21_600)
        with suppress(OSError, sqlite3.Error, RuntimeError, ValueError):
            await asyncio.to_thread(
                VIN_UNRESOLVED_STORE.complete_observer_attempt,
                job.vin,
                next_delay_seconds=retry_seconds,
                result="online_search_error",
            )
        logger.warning(
            "VIN-наблюдатель не смог выполнить поиск VIN …%s",
            job.vin[-6:],
            exc_info=True,
        )
        return "search_error"
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        logger.exception(
            "Ошибка VIN-наблюдателя для VIN …%s",
            job.vin[-6:],
        )
        with suppress(OSError, sqlite3.Error, RuntimeError, ValueError):
            await asyncio.to_thread(
                VIN_UNRESOLVED_STORE.complete_observer_attempt,
                job.vin,
                next_delay_seconds=3_600,
                result="observer_error",
            )
        return "observer_error"


async def vin_observer_loop(bot: object) -> None:
    await asyncio.sleep(VIN_OBSERVER_INITIAL_DELAY_SECONDS)
    while True:
        try:
            await run_vin_observer_once(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Непредвиденная ошибка фонового VIN-наблюдателя"
            )
        await asyncio.sleep(VIN_OBSERVER_INTERVAL_SECONDS)


async def application_post_init(application: Application) -> None:
    global VIN_OBSERVER_TASK
    if (
        VIN_OBSERVER_ENABLED
        and VIN_SEARCH_READY
        and VIN_UNRESOLVED_READY
        and VIN_ONLINE_SEARCHER.enabled
    ):
        VIN_OBSERVER_TASK = asyncio.create_task(
            vin_observer_loop(application.bot),
            name="vin-observer",
        )
        logger.info(
            "VIN-наблюдатель запущен: один VIN каждые %s сек., "
            "не более %s проверок в сутки",
            VIN_OBSERVER_INTERVAL_SECONDS,
            VIN_OBSERVER_DAILY_LIMIT,
        )


async def application_post_shutdown(application: Application) -> None:
    global VIN_OBSERVER_TASK
    if VIN_OBSERVER_TASK is None:
        return
    VIN_OBSERVER_TASK.cancel()
    with suppress(asyncio.CancelledError):
        await VIN_OBSERVER_TASK
    VIN_OBSERVER_TASK = None


async def handle_admin_vin_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    message = update.message
    user = update.effective_user
    if (
        message is None
        or message.text is None
        or user is None
        or user.id not in VIN_ADMIN_USER_IDS
    ):
        return False

    raw_text = message.text
    chat_id = message.chat_id
    reply = getattr(message, "reply_to_message", None)
    notification_vin = None
    if reply is not None and VIN_UNRESOLVED_READY:
        notification_vin = await asyncio.to_thread(
            VIN_UNRESOLVED_STORE.find_notification_vin,
            chat_id,
            reply.message_id,
        )
    if notification_vin is None and reply is not None:
        notification_vin = extract_vin(getattr(reply, "text", "") or "")

    direct_vin = extract_vin(raw_text)
    candidate = (
        notification_vin is not None
        or (direct_vin is not None and is_admin_reply_candidate(raw_text))
    )
    if not candidate:
        return False
    vin = notification_vin or direct_vin
    if vin is None:
        return False

    if not VIN_SEARCH_READY:
        await message.reply_text(
            "❌ VIN-база временно недоступна, результат не сохранён."
        )
        return True

    try:
        base_record = await asyncio.to_thread(VIN_STORE.lookup, vin)
        if is_admin_confirmation(raw_text):
            if base_record is None:
                raise VinAdminReplyError(
                    "Для этого VIN пока нет результата для подтверждения."
                )
            verified = confirm_admin_vin_record(base_record)
        else:
            verified = parse_admin_vin_reply(
                raw_text,
                vin=vin,
                base_record=base_record,
            )
        verified = await asyncio.to_thread(
            attach_catalog_articles,
            verified,
            DATABASE,
        )
        verified = await asyncio.to_thread(
            VIN_STORE.save_verified,
            verified,
        )
        if VIN_UNRESOLVED_READY:
            await asyncio.to_thread(VIN_UNRESOLVED_STORE.remove, vin)
        if context is not None:
            await deliver_manual_vin_result(context.bot, verified)
    except VinAdminReplyError as error:
        await message.reply_text(
            "❌ Не удалось сохранить VIN.\n"
            f"{error}\n\n"
            "Укажите номер одной строкой, например:\n"
            "OEM: A6560900380\n"
            "или Turbo P/N: KP39-015"
        )
        return True
    except (OSError, sqlite3.Error, RuntimeError, ValueError):
        logger.exception(
            "Не удалось сохранить ручной результат VIN …%s",
            vin[-6:],
        )
        await message.reply_text(
            "❌ Не удалось записать результат в VIN-базу. "
            "Данные не потеряны в вашем сообщении; попробуйте ещё раз."
        )
        return True

    await message.reply_text(
        "✅ Результат сохранён и отправлен пользователю.\n"
        f"VIN: {verified.vin}"
    )
    return True


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

    if await handle_admin_vin_reply(update, context):
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
        reverse_result = await asyncio.to_thread(
            DATABASE.reverse_search, user_input
        )
    except (OSError, sqlite3.Error, RuntimeError):
        logger.exception("Ошибка обратного поиска в SQLite")
        await update.message.reply_text(
            "❌ База данных временно недоступна. Попробуйте ещё раз позже."
        )
        return

    if reverse_result is not None:
        logger.info(
            "Режим поиска=reverse normalized=%s resolution=%s results=%s",
            reverse_result.normalized_query,
            reverse_result.resolution,
            len(reverse_result.numbers)
            if reverse_result.found
            else len(reverse_result.candidates),
        )
        for chunk in split_long_message(
            format_reverse_search_result(reverse_result),
            number_parts=True,
        ):
            await update.message.reply_text(chunk, parse_mode="HTML")
        return

    try:
        result = await asyncio.to_thread(DATABASE.search, user_input)
    except (OSError, sqlite3.Error, RuntimeError):
        logger.exception("Ошибка прямого поиска в SQLite")
        await update.message.reply_text(
            "❌ База данных временно недоступна. Попробуйте ещё раз позже."
        )
        return

    logger.info(
        "Режим поиска=direct normalized=%s results=%s",
        result.normalized_query,
        len(result.matches),
    )
    for chunk in split_long_message(format_search_result(result)):
        await update.message.reply_text(chunk, parse_mode="HTML")


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
        await message.reply_text(chunk, parse_mode="HTML")


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
        (
            "SQLite загружена: %s артикулов, %s номеров прямого поиска, "
            "%s обратных связей, %s кроссов, %s источников"
        ),
        stats.parts,
        stats.numbers,
        stats.reverse_numbers,
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
                "Онлайн-поиск VIN выключен: настройте Yandex Search API"
            )
        if YANDEX_VIN_SEARCHER.api_key and not YANDEX_VIN_SEARCHER.folder_id:
            logger.warning(
                "Yandex Search API не активирован: YANDEX_FOLDER_ID не задан"
            )
        if VIN_AGENT_ENABLED:
            logger.info(
                "Событийный Codex-наблюдатель включён: %s",
                VIN_AGENT_TRIGGER_URL,
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

    if VIN_ADMIN_USER_IDS:
        logger.info(
            "Ручная проверка VIN доступна: %s администраторов",
            len(VIN_ADMIN_USER_IDS),
        )
    else:
        logger.warning(
            "VIN_ADMIN_USER_IDS не задан: ручная проверка VIN выключена"
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
        .post_init(application_post_init)
        .post_shutdown(application_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CallbackQueryHandler(
            handle_vin_manual_review,
            pattern=f"^{VIN_MANUAL_CALLBACK_PREFIX}",
        )
    )
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("ТУРБОНАЙЗЕР запущен. Основной источник: %s", DATABASE.path)
    app.run_polling()


if __name__ == "__main__":
    main()
