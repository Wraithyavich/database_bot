import asyncio
import logging
import os
import sqlite3
import tempfile
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
from turbo_database import (
    DEFAULT_RESULT_LIMIT,
    MIN_PARTIAL_SEARCH_LENGTH,
    SearchResult,
    TurboDatabase,
    ensure_sqlite_database,
    normalize_number,
)


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
API_TOKEN = os.environ.get("API_TOKEN")
DEFAULT_DATABASE_DOWNLOAD_URL = (
    "https://media.githubusercontent.com/media/"
    "Wraithyavich/database_bot/master/turbo_parts.sqlite"
)


def resolve_database_path() -> Path:
    configured_path = os.environ.get("DATABASE_PATH")
    path = Path(configured_path) if configured_path else BASE_DIR / "turbo_search.sqlite"
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


DATABASE = TurboDatabase(resolve_database_path())
OCR_RECOGNIZER = RapidOcrRecognizer()
TELEGRAM_MESSAGE_LIMIT = 4000


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
        "Также можно отправить фотографию шильдика или номера.\n\n"
        f"Можно искать по части номера — минимум "
        f"{MIN_PARTIAL_SEARCH_LENGTH} символа. "
        "Дефисы, пробелы и регистр не имеют значения."
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")


async def handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if update.message is None or update.message.text is None:
        return

    user_input = clean_text(update.message.text)
    if not user_input:
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


async def handle_image(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if message is None:
        return

    if message.photo:
        file_id = message.photo[-1].file_id
        suffix = ".jpg"
    elif message.document is not None:
        file_id = message.document.file_id
        suffix = Path(message.document.file_name or "image.jpg").suffix or ".jpg"
    else:
        return

    try:
        telegram_file = await context.bot.get_file(file_id)
        with tempfile.TemporaryDirectory(prefix="turbo_ocr_") as temp_dir:
            image_path = Path(temp_dir) / f"image{suffix.lower()}"
            await telegram_file.download_to_drive(custom_path=image_path)
            candidates = await asyncio.to_thread(
                OCR_RECOGNIZER.recognize, image_path
            )
            matches = await asyncio.to_thread(
                search_image_candidates, DATABASE, candidates
            )
    except OcrUnavailableError:
        logger.exception("OCR-компонент не установлен")
        await message.reply_text(
            "❌ Поиск по изображению пока недоступен: OCR не установлен."
        )
        return
    except (OSError, RuntimeError, sqlite3.Error, TelegramError):
        logger.exception("Ошибка обработки изображения")
        await message.reply_text(
            "❌ Не удалось обработать изображение. "
            "Попробуйте другое фото без бликов и размытия."
        )
        return

    if not candidates:
        lines = [
            "❌ Не удалось распознать номер на изображении.",
            "Сфотографируйте шильдик крупнее, без бликов и размытия.",
        ]
    else:
        lines = format_image_search_results(matches)

    for chunk in split_long_message(lines):
        await message.reply_text(chunk)


def main() -> None:
    global DATABASE

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

    stats = DATABASE.validate()
    logger.info(
        "SQLite загружена: %s артикулов, %s номеров, %s кроссов, %s источников",
        stats.parts,
        stats.numbers,
        stats.crossrefs,
        stats.sources,
    )

    app = Application.builder().token(API_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image)
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("ТУРБОНАЙЗЕР запущен. Основной источник: %s", DATABASE.path)
    app.run_polling()


if __name__ == "__main__":
    main()
