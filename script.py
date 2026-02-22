import csv
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import os

# Получение токена
API_TOKEN = os.environ.get('API_TOKEN')
if API_TOKEN is None:
    try:
        from bot_token import API_TOKEN
    except ImportError:
        raise ValueError(
            "❌ Токен не найден! Задайте переменную окружения API_TOKEN "
            "или создайте файл bot_token.py с переменной API_TOKEN."
        )
# ---------- Очистка текста ----------
def clean_text(s):
    """Удаляет лишние пробелы, управляющие символы и BOM."""
    s = s.strip()
    s = s.replace('\r', '').replace('\n', '').replace('\ufeff', '')
    return ' '.join(s.split())

# ---------- Загрузка данных из CSV ----------
dict_by_col1 = defaultdict(list)   # ключ = первый столбец -> список значений второго
dict_by_col2 = defaultdict(list)   # ключ = второй столбец -> список значений первого

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
except FileNotFoundError:
    print("❌ Файл data.csv не найден! Поместите его в папку со скриптом.")
    exit(1)

print(f"✅ Загружено: {len(dict_by_col1)} уникальных ключей в первом столбце, {len(dict_by_col2)} во втором.")

# ---------- Получение токена ----------
# Токен хранится в отдельном файле bot_token.py, который не попадает в Git
try:
    from bot_token import TOKEN
except ImportError:
    raise ValueError("❌ Токен не найден! Создайте файл bot_token.py с переменной TOKEN.")

# ---------- Обработчики команд ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    emoji_id = "5247029251940586192"  # ваш ID кастомного эмодзи
    welcome_text = (
        f"<tg-emoji emoji-id=\"{emoji_id}\">😊</tg-emoji> ТУРБОНАЙЗЕР бот приветствует!\n"
        "Введите E&E P/N или Turbo P/N\n\n"
        "Пример: CT-VNT11B или 17201-52010"
    )
    await update.message.reply_text(welcome_text, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = clean_text(update.message.text)

    if user_input in dict_by_col2:
        values = dict_by_col2[user_input]
        reply = f"🔍 Найден E&E P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in values)
    elif user_input in dict_by_col1:
        values = dict_by_col1[user_input]
        reply = f"🔍 Найден Turbo P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in values)
    else:
        reply = "❌ Значение не найдено. Попробуйте другой запрос."

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