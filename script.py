import csv
import os
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- Получение токена из переменной окружения ----------
API_TOKEN = os.environ.get('API_TOKEN')
if API_TOKEN is None:
    raise ValueError("❌ Переменная окружения API_TOKEN не задана!")

# ---------- Константы ----------
MIN_SEARCH_LENGTH = 4          # минимальная длина для частичного поиска
MAX_RESULTS = 30                # максимальное количество результатов для показа
PREVIEW_RESULTS = 10            # сколько показать, если результатов больше MAX_RESULTS

# ---------- Очистка текста ----------
def clean_text(s):
    """Удаляет лишние пробелы, управляющие символы и BOM."""
    s = s.strip()
    s = s.replace('\r', '').replace('\n', '').replace('\ufeff', '')
    return ' '.join(s.split())

# ---------- Загрузка данных из CSV ----------
# Основные словари: оригинальный ключ -> список значений из другого столбца
dict_by_col1 = defaultdict(list)   # первый столбец (оригинал) -> список значений второго
dict_by_col2 = defaultdict(list)   # второй столбец (оригинал) -> список значений первого

# Словари для поиска без учёта регистра: ключ в нижнем регистре -> список оригинальных ключей
col1_lower_to_original = defaultdict(list)
col2_lower_to_original = defaultdict(list)

try:
    with open('data.csv', mode='r', encoding='utf-8-sig') as file:
        reader = csv.reader(file, delimiter=';')
        for row in reader:
            if len(row) >= 2:
                col1 = clean_text(row[0])
                col2 = clean_text(row[1])
                if col1 and col2:
                    dict_by_col1[col1].append(col2)
                    dict_by_col2[col2].append(col1)
                    col1_lower_to_original[col1.lower()].append(col1)
                    col2_lower_to_original[col2.lower()].append(col2)
except FileNotFoundError:
    print("❌ Файл data.csv не найден! Поместите его в папку со скриптом.")
    exit(1)

print(f"✅ Загружено: {len(dict_by_col1)} уникальных ключей в первом столбце, {len(dict_by_col2)} во втором.")

# ---------- Обработчики команд ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    emoji_id = "5247029251940586192"
    welcome_text = (
        f"<tg-emoji emoji-id=\"{emoji_id}\">😊</tg-emoji> ТУРБОНАЙЗЕР бот приветствует!\n"
        "Введите E&E P/N или Turbo P/N\n\n"
        "Пример: CT-VNT11B или 17201-52010\n\n"
        f"🔍 Можно искать по части номера (минимум {MIN_SEARCH_LENGTH} символа)."
    )
    await update.message.reply_text(welcome_text, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = clean_text(update.message.text)
    if not user_input:
        await update.message.reply_text("❌ Пустой запрос. Введите номер.")
        return

    user_input_lower = user_input.lower()
    input_len = len(user_input)

    # ---------- Точный поиск для коротких запросов ----------
    if input_len < MIN_SEARCH_LENGTH:
        if user_input_lower in col2_lower_to_original:
            original_keys = col2_lower_to_original[user_input_lower]
            values = []
            for key in original_keys:
                values.extend(dict_by_col2[key])
            unique_values = sorted(set(values))
            reply = f"🔍 Найден E&E P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in unique_values)
        elif user_input_lower in col1_lower_to_original:
            original_keys = col1_lower_to_original[user_input_lower]
            values = []
            for key in original_keys:
                values.extend(dict_by_col1[key])
            unique_values = sorted(set(values))
            reply = f"🔍 Найден Turbo P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in unique_values)
        else:
            reply = f"❌ Точное значение не найдено. Для поиска по части номера введите минимум {MIN_SEARCH_LENGTH} символа."
        await update.message.reply_text(reply)
        return

    # ---------- Частичный поиск ----------
    # Словарь для результатов из второго столбца: значение первого столбца -> множество суффиксов
    col2_results = defaultdict(set)
    # Множество для результатов из первого столбца (значения второго столбца)
    col1_results = set()

    # Поиск по второму столбцу (E&E P/N)
    for key_lower, original_keys in col2_lower_to_original.items():
        if user_input_lower in key_lower:
            for orig_key in original_keys:
                # Извлекаем суффикс (часть после последнего дефиса)
                suffix = orig_key.split('-')[-1] if '-' in orig_key else ''
                for val in dict_by_col2[orig_key]:
                    col2_results[val].add(suffix)

    # Поиск по первому столбцу (Turbo P/N)
    for key_lower, original_keys in col1_lower_to_original.items():
        if user_input_lower in key_lower:
            for orig_key in original_keys:
                for val in dict_by_col1[orig_key]:
                    col1_results.add(val)

    # Формируем ответ
    lines = []
    if col2_results:
        lines.append("🔍 По E&E P/N найдены Turbo P/N (суффиксы):")
        for val in sorted(col2_results.keys()):
            suffixes = sorted(col2_results[val])
            suffix_str = ", ".join(suffixes)
            lines.append(f"• {val} ({suffix_str})")
    if col1_results:
        lines.append("🔍 По Turbo P/N найдены E&E P/N:")
        for val in sorted(col1_results):
            lines.append(f"• {val}")

    if not lines:
        reply = f"❌ Ничего не найдено по запросу `{user_input}`."
    else:
        # Простая проверка на слишком длинный ответ (можно добавить ограничение)
        if len(lines) > MAX_RESULTS * 2:  # грубо
            # Обрежем до PREVIEW_RESULTS строк, но лучше обработать обе секции
            preview_lines = lines[:PREVIEW_RESULTS]
            reply = "\n".join(preview_lines) + f"\n... и ещё {len(lines) - PREVIEW_RESULTS}. Уточните запрос."
        else:
            reply = "\n".join(lines)

    await update.message.reply_text(reply)

# ---------- Запуск бота ----------
def main():
    app = Application.builder().token(API_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 Бот запущен и готов к работе...")
    app.run_polling()

if __name__ == '__main__':
    main()
