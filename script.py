import csv
import os
import re
from collections import defaultdict
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
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
    """Удаляет дефисы и приводит к нижнему регистру для сравнения."""
    return s.replace('-', '').lower()

def is_11_digit_number(s):
    return re.fullmatch(r'\d{11}', s) is not None

# ---------- Загрузка данных из CSV ----------
# Основные словари: оригинальный ключ -> список значений из другого столбца
dict_by_col1 = defaultdict(list)   # Turbo P/N -> список E&E P/N
dict_by_col2 = defaultdict(list)   # E&E P/N -> список Turbo P/N

# Словари для поиска по нормализованным ключам (без дефисов, нижний регистр)
col1_norm_to_original = defaultdict(list)   # нормализованный Turbo -> оригиналы
col2_norm_to_original = defaultdict(list)   # нормализованный E&E -> оригиналы

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
    print("❌ Файл data.csv не найден! Поместите его в папку со скриптом.")
    exit(1)

print(f"✅ Загружено: {len(dict_by_col1)} уникальных Turbo P/N, {len(dict_by_col2)} уникальных E&E P/N.")

# ---------- Функция для частичного поиска ----------
def partial_search(search_norm):
    """Выполняет частичный поиск по нормализованному запросу, возвращает множество Turbo P/N."""
    results = set()

    # Поиск по первому столбцу (Turbo P/N) – нашли ключ, берём значения из второго столбца
    for norm_key, original_keys in col1_norm_to_original.items():
        if search_norm in norm_key:
            for orig_key in original_keys:
                for val in dict_by_col1[orig_key]:
                    results.add(val)

    # Поиск по второму столбцу (E&E P/N) – нашли ключ, берём значения из первого столбца
    for norm_key, original_keys in col2_norm_to_original.items():
        if search_norm in norm_key:
            for orig_key in original_keys:
                for val in dict_by_col2[orig_key]:
                    results.add(val)

    return results

# ---------- Клавиатура ----------
def get_menu_keyboard():
    keyboard = [[KeyboardButton("Меню")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ---------- Обработчики ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    emoji_id = "5247029251940586192"  # ваш ID кастомного эмодзи (можно убрать)
    welcome_text = (
        f"<tg-emoji emoji-id=\"{emoji_id}\">😊</tg-emoji> ТУРБОНАЙЗЕР бот приветствует!\n"
        "Введите E&E P/N или Turbo P/N\n\n"
        "Пример: CT-VNT11B или 17201-52010\n\n"
        f"🔍 Можно искать по части номера (минимум {MIN_SEARCH_LENGTH} символа).\n"
        "Дефисы можно не ставить – бот поймёт."
    )
    await update.message.reply_text(welcome_text, parse_mode='HTML', reply_markup=get_menu_keyboard())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если пользователь нажал "Меню", вызываем start
    if update.message.text.strip() == "Меню":
        await start(update, context)
        return

    # Очищаем ввод
    user_input = clean_text(update.message.text)
    if not user_input:
        return

    # Нормализуем ввод (без дефисов, нижний регистр)
    user_input_norm = normalize(user_input)
    input_len = len(user_input_norm)

    # ---------- Точный поиск для коротких запросов (< MIN_SEARCH_LENGTH) ----------
    if input_len < MIN_SEARCH_LENGTH:
        # Сначала точное совпадение по второму столбцу (E&E P/N)
        if user_input_norm in col2_norm_to_original:
            original_keys = col2_norm_to_original[user_input_norm]
            values = set()
            for key in original_keys:
                values.update(dict_by_col2[key])
            reply = f"🔍 Найден E&E P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in sorted(values))
        # Точное совпадение по первому столбцу (Turbo P/N)
        elif user_input_norm in col1_norm_to_original:
            original_keys = col1_norm_to_original[user_input_norm]
            values = set()
            for key in original_keys:
                values.update(dict_by_col1[key])
            reply = f"🔍 Найден Turbo P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in sorted(values))
        else:
            reply = f"❌ Точное значение не найдено. Для поиска по части номера введите минимум {MIN_SEARCH_LENGTH} символа."
        await update.message.reply_text(reply, reply_markup=get_menu_keyboard())
        return

    # ---------- Частичный поиск (длина >= MIN_SEARCH_LENGTH) ----------
    results = partial_search(user_input_norm)

    # Если ничего не найдено, пробуем заменить среднюю часть на 970 для 11-значных номеров
    if not results and is_11_digit_number(user_input_norm):
        first4 = user_input_norm[:4]
        middle3 = user_input_norm[4:7]
        last4 = user_input_norm[7:]
        if middle3 != '970':
            new_norm = first4 + '970' + last4
            results = partial_search(new_norm)
            if results:
                lines = [f"• {v}" for v in sorted(results)]
                reply = f"🔍 Результаты для `{user_input}`:\n" + "\n".join(lines)
                await update.message.reply_text(reply, reply_markup=get_menu_keyboard())
                return

    if not results:
        reply = f"❌ Ничего не найдено по запросу `{user_input}`."
    else:
        # Группируем по Turbo P/N с суффиксами? Но в первом боте мы выводим просто список Turbo P/N.
        # По предыдущим версиям мы выводили уникальные Turbo P/N.
        lines = [f"• {v}" for v in sorted(results)]
        reply = f"🔍 Результаты поиска для `{user_input}`:\n" + "\n".join(lines)

    await update.message.reply_text(reply, reply_markup=get_menu_keyboard())

def main():
    app = Application.builder().token(API_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 ТУРБОНАЙЗЕР бот с кнопкой Меню запущен...")
    app.run_polling()

if __name__ == '__main__':
    main()
