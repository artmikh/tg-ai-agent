import os
import asyncio
import sqlite3
import json
import yaml
import re  # Для локального фильтра
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.enums import ParseMode  # Правильный импорт для форматирования
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import OpenAI

# Загружаем переменные
load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Умное определение ID (число или @username)
raw_channel_id = os.getenv("MY_CHANNEL_ID")
if not raw_channel_id:
    print("ОШИБКА: MY_CHANNEL_ID не указан в .env")
    exit(1)
if raw_channel_id.startswith('@'):
    MY_CHANNEL_ID = raw_channel_id
else:
    try:
        MY_CHANNEL_ID = int(raw_channel_id)
    except ValueError:
        print("ОШИБКА: MY_CHANNEL_ID имеет неверный формат. Используйте @username или цифровой ID.")
        exit(1)

TARGET_CHANNELS = [ch.strip() for ch in os.getenv("TARGET_CHANNELS", "").split(",") if ch.strip()]
KEYWORDS = [kw.strip() for kw in os.getenv("KEYWORDS", "").split(",") if kw.strip()]

# Загружаем конфиг
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

# Инициализируем OpenAI
ai_client = OpenAI(api_key=OPENAI_API_KEY)

# --- БД ---
DB_PATH = "/app/data/agent_db.sqlite"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS processed_messages (
                    channel_id TEXT, message_id INTEGER, PRIMARY KEY (channel_id, message_id))''')
    conn.commit()
    conn.close()

def is_processed(channel_id, message_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute('SELECT 1 FROM processed_messages WHERE channel_id=? AND message_id=?', (str(channel_id), message_id))
    res = cur.fetchone()
    conn.close()
    return res is not None

def save_processed(channel_id, message_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute('INSERT OR IGNORE INTO processed_messages VALUES (?, ?)', (str(channel_id), message_id))
    conn.commit()
    conn.close()

# --- ЛОКАЛЬНЫЙ ФИЛЬТР ---
def local_keyword_filter(text, keywords):
    """Быстрый поиск ключевых слов в тексте без учета регистра"""
    if not keywords:
        return True
    pattern = r'(?:' + '|'.join(map(re.escape, keywords)) + r')'
    if re.search(pattern, text, re.IGNORECASE):
        return True
    return False

# --- ИИ ---
def check_relevance(text):
    prompt = config['prompts']['filter'].format(keywords=", ".join(KEYWORDS), text=text)
    try:
        resp = ai_client.chat.completions.create(
            model=config['ai']['model'],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        return resp.choices[0].message.content.strip().upper() == "YES"
    except Exception as e:
        print(f"OpenAI Filter Error: {e}")
        return False

def format_message(text, source_channel):
    prompt = config['prompts']['formatter'].format(text=text)
    try:
        resp = ai_client.chat.completions.create(
            model=config['ai']['model'],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        raw_text = resp.choices[0].message.content.strip()
        
        # Чистим ответ от маркдаун-блоков
        if raw_text.startswith("```json"):
            raw_text = raw_text[7:]
        if raw_text.startswith("```"):
            raw_text = raw_text[3:]
        if raw_text.endswith("```"):
            raw_text = raw_text[:-3]
            
        clean_json = raw_text.strip()
        data = json.loads(clean_json)
        
        # ИИ может вернуть один объект {} или массив [{}]. Приводим всё к массиву
        if isinstance(data, dict):
            items = [data]
        elif isinstance(data, list):
            items = data
        else:
            return []
            
        template = config['output_template']
        results = []
        
        for item in items:
            # Добавляем имя канала-источника
            item["source_channel"] = source_channel
            
            # Экранируем фигурные скобки в значениях ИИ, чтобы не ломался .format()
            safe_item = {}
            for key, value in item.items():
                if isinstance(value, str):
                    safe_item[key] = value.replace("{", "{{").replace("}", "}}")
                else:
                    safe_item[key] = value
            
            # Форматируем
            formatted_text = template.format(**safe_item)
            results.append(formatted_text)
            
        return results
        
    except Exception as e:
        print(f"OpenAI Format Error: {e}")
        return []

# --- АГЕНТ ---
async def agent_job(userbot: Client, bot: Client):
    print(f"[{datetime.now()}] Запуск парсинга...")
    # Берем глубину поиска из конфига (по умолчанию 24 часа, если не указано)
    scan_hours = config['parsing'].get('scan_depth_hours', 24)
    time_threshold = datetime.now() - timedelta(hours=scan_hours)
    
    # Берем лимит из конфига
    limit = config['parsing']['messages_per_channel']
    delay = config['parsing']['delay_between_channels']
    schedule_mins = config['parsing']['schedule_minutes']

    for channel in TARGET_CHANNELS:
        print(f"Чтение: {channel} (лимит: {limit})")
        try:
            async for message in userbot.get_chat_history(channel, limit=limit):
                # Проверяем дату
                if message.date.replace(tzinfo=None) < time_threshold:
                    break
                
                # Берем текст или подпись к медиа
                text = message.text or message.caption
                if not text or is_processed(channel, message.id):
                    continue

                # ШАГ 1: Локальный фильтр
                if not local_keyword_filter(text, KEYWORDS):
                    continue
                
                # ШАГ 2: ИИ-фильтр
                if check_relevance(text):
                    print(f"Совпадение в {channel}!")
                    
                    # ШАГ 3: Форматирование
                    formatted_messages = format_message(text, f"@{channel}")
                    
                    if formatted_messages:
                        # ШАГ 4: Отправка каждой позиции отдельно
                        for msg_text in formatted_messages:
                            try:
                                await bot.send_message(
                                    chat_id=MY_CHANNEL_ID,
                                    text=msg_text,
                                    parse_mode=ParseMode.HTML
                                )
                                print("Успешно отправлено в канал.")
                                await asyncio.sleep(1) # Пауза 1 сек между сообщениями
                            except Exception as send_err:
                                print(f"Ошибка отправки: {send_err}")
                    
                    # Сохраняем ID исходного сообщения, чтобы не парсить повторно
                    save_processed(channel, message.id)
                    
        except Exception as e:
            print(f"Ошибка чтения {channel}: {e}")
        
        await asyncio.sleep(delay)

    print(f"[{datetime.now()}] Парсинг завершен. Следующий запуск через {schedule_mins} мин.")

async def main():
    init_db()

    # Юзербот для чтения
    userbot = Client("user", api_id=API_ID, api_hash=API_HASH, workdir="/app/data")
    # Бот для отправки
    bot = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir="/app/data")

    schedule_mins = config['parsing']['schedule_minutes']
    scheduler = AsyncIOScheduler()
    scheduler.add_job(agent_job, 'interval', minutes=schedule_mins, args=[userbot, bot])

    async with userbot, bot:
        print("Клиенты (Юзербот и Бот) запущены!")
        scheduler.start()
        # Сразу запускаем первую проверку
        await agent_job(userbot, bot)
        # Держим скрипт работающим бесконечно
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())