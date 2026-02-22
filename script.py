import csv
import os
import re
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- Получение токена из переменной окружения ----------
API_TOKEN = os.environ.get('API_TOKEN')
if API_TOKEN is None:
    raise ValueError("❌ Переменная окружения API_TOKEN не задана!")

# ---------- Константы ----------
MIN_SEARCH_LENGTH = 4          # минимальная длина для частичного поиска

# ---------- Очистка текста ----------
def clean_text(s):
    """Удаляет лишние пробелы, управляющие символы и BOM."""
    s = s.strip()
    s = s.replace('\r', '').replace('\n', '').replace('\ufeff', '')
    return ' '.join(s.split())

def normalize(s):
    """Удаляет дефисы и приводит к нижнему регистру для сравнения."""
    return s.replace('-', '').lower()

def is_11_digit_number(s):
    """Проверяет, состоит ли строка ровно из 11 цифр (после удаления дефисов)."""
    return re.fullmatch(r'\d{11}', s) is not None

# ---------- Загрузка данных из CSV ----------
# Основные словари: оригинальный ключ -> список значений из другого столбца
dict_by_col1 = defaultdict(list)   # первый столбец (оригинал) -> список значений второго
dict_by_col2 = defaultdict(list)   # второй столбец (оригинал) -> список значений первого

# Словари для поиска по нормализованным ключам (без дефисов, нижний регистр)
col1_norm_to_original = defaultdict(list)   # нормализованный ключ первого столбца -> список оригинальных ключей
col2_norm_to_original = defaultdict(list)   # нормализованный ключ второго столбца -> список оригинальных ключей

# Для обратной совместимости с регистронезависимым поиском (с дефисами) – оставляем
col1_lower_to_original = defaultdict(list)  # нижний регистр с дефисами -> список оригинальных ключей
col2_lower_to_original = defaultdict(list)

try:
    with open('data.csv', mode='r', encoding='utf-8-sig') as file:
        reader = csv.reader(file, delimiter=';')
        for row in reader:
            if len(row) >= 2:
                col1 = clean_text(row[0])
                col2 = clean_text(row[1])
                if col1 and col2:
                    # Заполняем основные словари
                    dict_by_col1[col1].append(col2)
                    dict_by_col2[col2].append(col1)

                    # Для поиска с учётом регистра и дефисов (нижний регистр)
                    col1_lower_to_original[col1.lower()].append(col1)
                    col2_lower_to_original[col2.lower()].append(col2)

                    # Для поиска без дефисов (нормализованные)
                    col1_norm = normalize(col1)
                    col2_norm = normalize(col2)
                    col1_norm_to_original[col1_norm].append(col1)
                    col2_norm_to_original[col2_norm].append(col2)
except FileNotFoundError:
    print("❌ Файл data.csv не найден! Поместите его в папку со скриптом.")
    exit(1)

print(f"✅ Загружено: {len(dict_by_col1)} уникальных ключей в первом столбце, {len(dict_by_col2)} во втором.")

# ---------- Обработчики команд ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    emoji_id = "5247029251940586192"  # ваш ID кастомного эмодзи
    welcome_text = (
        f"<tg-emoji emoji-id=\"{emoji_id}\">😊</tg-emoji> ТУРБОНАЙЗЕР бот приветствует!\n"
        "Введите E&E P/N или Turbo P/N\n\n"
        "Пример: CT-VNT11B или 17201-52010\n\n"
        f"🔍 Можно искать по части номера (минимум {MIN_SEARCH_LENGTH} символа).\n"
        "Дефисы можно не ставить – бот поймёт."
    )
    await update.message.reply_text(welcome_text, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Очищаем ввод
    user_input = clean_text(update.message.text)
    if not user_input:
        await update.message.reply_text("❌ Пустой запрос. Введите номер.")
        return

    # Нормализуем ввод (без дефисов, нижний регистр)
    user_input_norm = normalize(user_input)
    input_len = len(user_input_norm)

    # ---------- Вспомогательная функция для частичного поиска ----------
    def partial_search(search_norm):
        """Выполняет частичный поиск по нормализованному запросу, возвращает col1_results, col2_results."""
        col1_res = defaultdict(set)
        col2_res = defaultdict(set)

        # Поиск по первому столбцу (Turbo P/N)
        for norm_key, original_keys in col1_norm_to_original.items():
            if search_norm in norm_key:
                for orig_key in original_keys:
                    for val in dict_by_col1[orig_key]:
                        suffix = val.split('-')[-1] if '-' in val else val
                        col1_res[orig_key].add(suffix)

        # Поиск по второму столбцу (E&E P/N)
        for norm_key, original_keys in col2_norm_to_original.items():
            if search_norm in norm_key:
                for orig_key in original_keys:
                    for val in dict_by_col2[orig_key]:
                        suffix = orig_key.split('-')[-1] if '-' in orig_key else orig_key
                        col2_res[val].add(suffix)

        return col1_res, col2_res

    # ---------- Вспомогательная функция для форматирования результатов ----------
    def format_results(col1_res, col2_res, search_query):
        lines = []
        if col1_res:
            lines.append(f"🔍 По Turbo P/N найдены E&E P/N ({search_query}):")
            for key in sorted(col1_res.keys()):
                suffixes = sorted(col1_res[key])
                lines.append(f"• {key} ({', '.join(suffixes)})")
        if col2_res:
            lines.append(f"🔍 По E&E P/N найдены Turbo P/N ({search_query}):")
            for key in sorted(col2_res.keys()):
                suffixes = sorted(col2_res[key])
                lines.append(f"• {key} ({', '.join(suffixes)})")
        return "\n".join(lines)

    # ---------- Точный поиск для коротких запросов ----------
    if input_len < MIN_SEARCH_LENGTH:
        # Сначала точное совпадение по второму столбцу (E&E P/N) – ищем по нормализованному ключу
        if user_input_norm in col2_norm_to_original:
            original_keys = col2_norm_to_original[user_input_norm]
            values = []
            for key in original_keys:
                values.extend(dict_by_col2[key])
            unique_values = sorted(set(values))
            reply = f"🔍 Найден E&E P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in unique_values)
        # Точное совпадение по первому столбцу (Turbo P/N)
        elif user_input_norm in col1_norm_to_original:
            original_keys = col1_norm_to_original[user_input_norm]
            values = []
            for key in original_keys:
                values.extend(dict_by_col1[key])
            unique_values = sorted(set(values))
            reply = f"🔍 Найден Turbo P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in unique_values)
        else:
            reply = f"❌ Точное значение не найдено. Для поиска по части номера введите минимум {MIN_SEARCH_LENGTH} символа."
        await update.message.reply_text(reply)
        return

    # ---------- Частичный поиск (длина >= MIN_SEARCH_LENGTH) ----------
    col1_results, col2_results = partial_search(user_input_norm)

    if not col1_results and not col2_results:
        # Ничего не найдено – пробуем заменить среднюю часть на 970, если формат подходит
        if is_11_digit_number(user_input_norm):
            first4 = user_input_norm[:4]
            middle3 = user_input_norm[4:7]
            last4 = user_input_norm[7:]
            if middle3 != '970':
                new_norm = first4 + '970' + last4
                col1_results_new, col2_results_new = partial_search(new_norm)
                if col1_results_new or col2_results_new:
                    reply = format_results(col1_results_new, col2_results_new, user_input)
                    await update.message.reply_text(reply)
                    return
        # Если и замена не помогла
        reply = f"❌ Ничего не найдено по запросу `{user_input}`."
    else:
        reply = format_results(col1_results, col2_results, user_input)

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
