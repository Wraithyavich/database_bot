import csv
import os
import re
import requests
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- Получение токена из переменной окружения ----------
API_TOKEN = os.environ.get('API_TOKEN')
if API_TOKEN is None:
    raise ValueError("❌ Переменная окружения API_TOKEN не задана!")

# ---------- Константы ----------
MIN_SEARCH_LENGTH = 4
DATA_FILE = 'data.csv'
JRONE_FILE = 'jronecross.csv'

# ---------- Очистка текста ----------
def clean_text(s):
    s = s.strip()
    s = s.replace('\r', '').replace('\n', '').replace('\ufeff', '')
    return ' '.join(s.split())

# ---------- Замена кириллических букв, похожих на латиницу ----------
CYRILLIC_TO_LATIN = {
    'А': 'A', 'а': 'a',
    'В': 'B', 'в': 'b',
    'Е': 'E', 'е': 'e',
    'К': 'K', 'к': 'k',
    'М': 'M', 'м': 'm',
    'Н': 'H', 'н': 'h',
    'О': 'O', 'о': 'o',
    'Р': 'P', 'р': 'p',
    'С': 'C', 'с': 'c',
    'Т': 'T', 'т': 't',
    'У': 'Y', 'у': 'y',
    'Х': 'X', 'х': 'x',
}

def replace_cyrillic_like_latin(s):
    """Заменяет кириллические символы на похожие латинские."""
    return ''.join(CYRILLIC_TO_LATIN.get(ch, ch) for ch in s)

def normalize(s):
    """Удаляет дефисы, приводит к нижнему регистру, предварительно заменяя кириллицу."""
    s = replace_cyrillic_like_latin(s)
    return s.replace('-', '').lower()

def is_11_digit_number(s):
    return re.fullmatch(r'\d{11}', s) is not None

# ---------- Взаимодействие с третьим ботом (парсер VIN) ----------
def get_turbo_by_vin(vin):
    try:
        # Третий бот доступен по внутреннему адресу vin-parser на порту 3000
        url = f"http://vin-parser:3000/search?vin={vin}"
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            return response.json().get("articles", [])
        else:
            return []
    except Exception as e:
        print(f"Ошибка при вызове vin-parser: {e}")
        return []

async def ping_vin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для проверки связи с третьим ботом."""
    try:
        url = "http://vin-parser:3000/search?vin=TEST123"
        response = requests.get(url, timeout=5)
        await update.message.reply_text(f"Статус: {response.status_code}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

# ---------- Загрузка основной базы (data.csv) ----------
dict_by_col1 = defaultdict(list)   # Turbo P/N -> список E&E P/N
dict_by_col2 = defaultdict(list)   # E&E P/N -> список Turbo P/N
col1_norm_to_original = defaultdict(list)  # нормализованный Turbo -> оригиналы
col2_norm_to_original = defaultdict(list)  # нормализованный E&E -> оригиналы

try:
    with open(DATA_FILE, mode='r', encoding='utf-8-sig') as file:
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

print(f"✅ Основная база: {len(dict_by_col1)} Turbo P/N, {len(dict_by_col2)} E&E P/N.")

# ---------- Загрузка базы JRN-кроссов (jronecross.csv) ----------
jrone_norm_to_art = defaultdict(set)
jrone_original_info = {}

try:
    with open(JRONE_FILE, mode='r', encoding='utf-8-sig') as file:
        reader = csv.reader(file, delimiter=';')
        for row in reader:
            if len(row) >= 3:
                jrone = clean_text(row[0])
                our_number = clean_text(row[1])
                our_art = clean_text(row[2])
                if jrone and our_art:
                    norm = normalize(jrone)
                    jrone_norm_to_art[norm].add(our_art)
                    if jrone not in jrone_original_info:
                        jrone_original_info[jrone] = []
                    jrone_original_info[jrone].append((our_number, our_art))
except FileNotFoundError:
    print("⚠️ Файл jronecross.csv не найден, поиск по JRN-номерам недоступен.")
except Exception as e:
    print(f"❌ Ошибка загрузки {JRONE_FILE}: {e}")

print(f"✅ JRN-база: {len(jrone_norm_to_art)} уникальных нормализованных JRN-номеров.")

# ---------- Функция частичного поиска в основной базе ----------
def partial_search_main(search_norm):
    results = set()
    for norm_key, original_keys in col1_norm_to_original.items():
        if search_norm in norm_key:
            for orig_key in original_keys:
                for val in dict_by_col1[orig_key]:
                    results.add(val)
    for norm_key, original_keys in col2_norm_to_original.items():
        if search_norm in norm_key:
            for orig_key in original_keys:
                for val in dict_by_col2[orig_key]:
                    results.add(val)
    return results

# ---------- Обработчики ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    emoji_id = "5247029251940586192"
    welcome_text = (
        f"<tg-emoji emoji-id=\"{emoji_id}\">😊</tg-emoji> ТУРБОНАЙЗЕР бот приветствует!\n"
        "Введите E&E P/N, Turbo P/N или JRN-номер\n\n"
        "Пример: CT-VNT11B или 17201-52010\n\n"
        f"🔍 Можно искать по части номера (минимум {MIN_SEARCH_LENGTH} символа).\n"
        "Дефисы можно не ставить – бот поймёт.\n"
        "Также бот понимает русские буквы, похожие на латинские (например, Е = E, Н = H).\n"
       # "Если ввести VIN-номер (17 символов), бот попытается найти артикулы турбин через внешний сервис."
    )
    await update.message.reply_text(welcome_text, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = clean_text(update.message.text)
    if not user_input:
        return

    # ---- Проверка на VIN (очищенный от лишних символов) ----
    vin_cleaned = re.sub(r'[^A-Za-z0-9]', '', user_input)
    if len(vin_cleaned) == 17 and re.search(r'[A-Za-z]', vin_cleaned) and re.search(r'\d', vin_cleaned):
        articles = get_turbo_by_vin(vin_cleaned)
        if articles:
            await update.message.reply_text(
                f"🔍 По VIN найдены артикулы турбин:\n" + "\n".join(f"• {a}" for a in articles)
            )
            return
        #else:
            # Если ничего не найдено – просто покажем сообщение и продолжим обычный поиск (опционально можно прервать)
            #await update.message.reply_text("❌ По данному VIN ничего не найдено. Пробую поиск по локальным базам...")
            # Не выходим, чтобы попробовать поиск по базам

    # ---- Основной поиск по базам ----
    user_input_norm = normalize(user_input)
    input_len = len(user_input_norm)

    # Поиск по JRN-базе
    jrone_arts = set()
    if input_len < MIN_SEARCH_LENGTH:
        if user_input_norm in jrone_norm_to_art:
            jrone_arts = jrone_norm_to_art[user_input_norm]
    else:
        for norm_key, arts in jrone_norm_to_art.items():
            if user_input_norm in norm_key:
                jrone_arts.update(arts)

    if jrone_arts:
        lines = []
        for art in sorted(jrone_arts):
            if art in dict_by_col1:
                eee_list = sorted(set(dict_by_col1[art]))
                lines.append(f"• {art} → {', '.join(eee_list)}")
            elif art in dict_by_col2:
                turbo_list = sorted(set(dict_by_col2[art]))
                lines.append(f"• {art} → {', '.join(turbo_list)}")
            else:
                lines.append(f"• {art} (нет в основной базе)")
        reply = f"🔍 По JRN-номеру `{user_input}` найдены артикулы:\n" + "\n".join(lines)
        await update.message.reply_text(reply)
        return

    # Поиск в основной базе
    if input_len < MIN_SEARCH_LENGTH:
        if user_input_norm in col2_norm_to_original:
            original_keys = col2_norm_to_original[user_input_norm]
            values = set()
            for key in original_keys:
                values.update(dict_by_col2[key])
            reply = f"🔍 Найден E&E P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in sorted(values))
        elif user_input_norm in col1_norm_to_original:
            original_keys = col1_norm_to_original[user_input_norm]
            values = set()
            for key in original_keys:
                values.update(dict_by_col1[key])
            reply = f"🔍 Найден Turbo P/N для `{user_input}`:\n" + "\n".join(f"• {v}" for v in sorted(values))
        else:
            reply = f"❌ Точное значение не найдено. Для поиска по части номера введите минимум {MIN_SEARCH_LENGTH} символа."
        await update.message.reply_text(reply)
        return

    # Частичный поиск
    results = partial_search_main(user_input_norm)

    # Если ничего не найдено, пробуем заменить среднюю часть на 970 для 11-значных номеров
    if not results and is_11_digit_number(user_input_norm):
        first4 = user_input_norm[:4]
        middle3 = user_input_norm[4:7]
        last4 = user_input_norm[7:]
        if middle3 != '970':
            new_norm = first4 + '970' + last4
            results = partial_search_main(new_norm)
            if results:
                lines = [f"• {v}" for v in sorted(results)]
                reply = f"🔍 Результаты для `{user_input}`:\n" + "\n".join(lines)
                await update.message.reply_text(reply)
                return

    if not results:
        reply = f"❌ Ничего не найдено по запросу `{user_input}`."
    else:
        lines = [f"• {v}" for v in sorted(results)]
        reply = f"🔍 Результаты поиска для `{user_input}`:\n" + "\n".join(lines)

    await update.message.reply_text(reply)

def main():
    app = Application.builder().token(API_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping_vin", ping_vin))  # команда для проверки связи с третьим ботом
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 ТУРБОНАЙЗЕР бот с JRN-кроссами ...")
    app.run_polling()

if __name__ == '__main__':
    main()
