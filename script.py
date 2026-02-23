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
MIN_SEARCH_LENGTH = 4

# ---------- Очистка текста ----------
def clean_text(s):
    s = s.strip()
    s = s.replace('\r', '').replace('\n', '').replace('\ufeff', '')
    return ' '.join(s.split())

def normalize(s):
    return s.replace('-', '').lower()

def is_11_digit_number(s):
    return re.fullmatch(r'\d{11}', s) is not None

# ---------- Загрузка данных из CSV ----------
dict_by_col1 = defaultdict(list)
dict_by_col2 = defaultdict(list)
col1_norm_to_original = defaultdict(list)
col2_norm_to_original = defaultdict(list)

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
                    col1_norm_to_original[normalize(col1)].append(col1)
                    col2_norm_to_original[normalize(col2)].append(col2)
except FileNotFoundError:
    print("❌ Файл data.csv не найден!")
    exit(1)

print(f"✅ Загружено: {len(dict_by_col1)} уникальных ключей в первом столбце, {len(dict_by_col2)} во втором.")

# ---------- Функции поиска ----------
def find_exact_original_art(query):
    norm = normalize(query)
    if norm in col2_norm_to_original:
        return col2_norm_to_original[norm][0]  # возвращаем первый, хотя может быть несколько
    if norm in col1_norm_to_original:
        return col1_norm_to_original[norm][0]
    return None

def partial_search(query):
    norm = normalize(query)
    if len(norm) < MIN_SEARCH_LENGTH:
        return []
    results = set()
    # по второму столбцу
    for norm_key, orig_keys in col2_norm_to_original.items():
        if norm in norm_key:
            for orig in orig_keys:
                for val in dict_by_col2[orig]:
                    results.add(val)
    # по первому столбцу
    for norm_key, orig_keys in col1_norm_to_original.items():
        if norm in norm_key:
            for orig in orig_keys:
                for val in dict_by_col1[orig]:
                    results.add(val)
    return sorted(results)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    emoji_id = "5247029251940586192"
    welcome_text = (
        f"<tg-emoji emoji-id=\"{emoji_id}\">😊</tg-emoji> ТУРБОНАЙЗЕР бот приветствует!\n"
        "Введите E&E P/N или Turbo P/N\n\n"
        f"Пример: CT-VNT11B или 17201-52010\n\n"
        f"🔍 Можно искать по части номера (минимум {MIN_SEARCH_LENGTH} символа)."
    )
    await update.message.reply_text(welcome_text, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = clean_text(update.message.text)
    if not user_input:
        return

    user_input_norm = normalize(user_input)
    input_len = len(user_input_norm)

    def partial_search_query(norm_query):
        results = set()
        for norm_key, orig_keys in col2_norm_to_original.items():
            if norm_query in norm_key:
                for orig in orig_keys:
                    for val in dict_by_col2[orig]:
                        results.add(val)
        for norm_key, orig_keys in col1_norm_to_original.items():
            if norm_query in norm_key:
                for orig in orig_keys:
                    for val in dict_by_col1[orig]:
                        results.add(val)
        return sorted(results)

    # Точный поиск для коротких запросов
    if input_len < MIN_SEARCH_LENGTH:
        if user_input_norm in col2_norm_to_original:
            orig_keys = col2_norm_to_original[user_input_norm]
            values = set()
            for key in orig_keys:
                values.update(dict_by_col2[key])
            reply = f"🔍 Найден E&E P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in sorted(values))
        elif user_input_norm in col1_norm_to_original:
            orig_keys = col1_norm_to_original[user_input_norm]
            values = set()
            for key in orig_keys:
                values.update(dict_by_col1[key])
            reply = f"🔍 Найден Turbo P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in sorted(values))
        else:
            reply = f"❌ Точное значение не найдено. Для поиска по части номера введите минимум {MIN_SEARCH_LENGTH} символа."
        await update.message.reply_text(reply)
        return

    # Частичный поиск
    candidates = partial_search_query(user_input_norm)

    if not candidates:
        # Пробуем заменить среднюю часть на 970, если подходит
        if is_11_digit_number(user_input_norm):
            first4 = user_input_norm[:4]
            middle3 = user_input_norm[4:7]
            last4 = user_input_norm[7:]
            if middle3 != '970':
                new_norm = first4 + '970' + last4
                candidates = partial_search_query(new_norm)
                if candidates:
                    reply = f"🔍 По E&E P/N найдены Turbo P/N ({user_input}):\n" + "\n".join(f"• {v}" for v in candidates)
                    await update.message.reply_text(reply)
                    return

    if not candidates:
        reply = f"❌ Ничего не найдено по запросу `{user_input}`."
    else:
        # Определяем, по какому столбцу ищем (можно просто вывести как результат)
        # Для единообразия выводим просто список найденных значений
        reply = f"🔍 Результаты поиска для `{user_input}`:\n" + "\n".join(f"• {v}" for v in candidates)

    await update.message.reply_text(reply)

def main():
    app = Application.builder().token(API_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 Бот запущен...")
    app.run_polling()

if __name__ == '__main__':
    main()
