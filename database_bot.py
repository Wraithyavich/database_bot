import csv
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update, MessageEntity
from bot_token import TOKEN
from bot_token import emoji_id
def clean_text(s):
    """Очищает строку от лишних пробелов, управляющих символов и BOM."""
    s = s.strip()
    s = s.replace('\r', '').replace('\n', '').replace('\ufeff', '')
    # заменяем множественные пробелы/табуляции на один пробел (опционально)
    s = ' '.join(s.split())
    return s

# Словари для двунаправленного поиска
dict_by_col1 = defaultdict(list)   # ключ = значение первого столбца -> список значений второго
dict_by_col2 = defaultdict(list)   # ключ = значение второго столбца -> список значений первого

try:
    with open('data.csv', mode='r', encoding='utf-8-sig') as file:
        reader = csv.reader(file, delimiter=';')   # разделитель — точка с запятой
        for row in reader:
            if len(row) >= 2:
                col1 = clean_text(row[0])
                col2 = clean_text(row[1])
                if col1 and col2:                  # пропускаем пустые после очистки
                    dict_by_col1[col1].append(col2)
                    dict_by_col2[col2].append(col1)
except FileNotFoundError:
    print("❌ Файл data.csv не найден! Поместите его в папку со скриптом.")
    exit(1)

print(f"✅ Загружено: {len(dict_by_col1)} уникальных ключей в первом столбце, {len(dict_by_col2)} во втором.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        f"<tg-emoji emoji-id=\"{emoji_id}\">😊</tg-emoji> ТУРБОНАЙЗЕР бот приветствует!\n"
        "Введите E&E P/N или Turbo P/N\n\n"
        "Пример: CT-VNT11B или 17201-52010"
    )
    await update.message.reply_text(welcome_text, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений."""
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




def main():
    # Создаём приложение
    app = Application.builder().token(TOKEN).build()

    # Добавляем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Запускаем бота
    print("🚀 Бот запущен и готов к работе...")
    app.run_polling()
if __name__ == '__main__':
    main()