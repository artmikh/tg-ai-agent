import os
import asyncio
import sqlite3
import json
import requests  # Добавили requests
import yaml
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import OpenAI

# Загружаем переменные
load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MY_CHANNEL_ID = os.getenv("MY_CHANNEL_ID") # Оставляем строкой
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

# --- Отправка сообщений через Bot API ---
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload, timeout=10)
        result = response.json()
        if not result.get("ok"):
            print(f"Ошибка отправки в Telegram: {result.get('description')}")
    except Exception as e:
        print(f"Сбой сети при отправке: {e}")

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

def format_message(text):
    prompt = config['prompts']['formatter'].format(text=text)
    try:
        resp = ai_client.chat.completions.create(
            model=config['ai']['model'],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        data = json.loads(resp.choices[0].message.content.strip())
        template = config['output_template']
        return template.format(
            product=data.get("product", "Не указано"),
            price=data.get("price", "Не указано"),
            contact=data.get("contact", "Не указано"),
            details=data.get("details", "Не указано")
        )
    except json.JSONDecodeError:
        print("ИИ вернул некорректный JSON. Пропускаем форматирование.")
        return None
    except Exception as e:
        print(f"OpenAI Format Error: {e}")
        return None

# --- Агент ---
async def agent_job(userbot: Client):
    print(f"[{datetime.now()}] Запуск парсинга...")
    schedule_mins = config['parsing']['schedule_minutes']
    time_threshold = datetime.now() - timedelta(minutes=schedule_mins)
    limit = config['parsing']['messages_per_channel']
    delay = config['parsing']['delay_between_channels']

    for channel in TARGET_CHANNELS:
        print(f"Чтение: {channel}")
        try:
            async for message in userbot.get_chat_history(channel, limit=limit):
                if message.date.replace(tzinfo=None) < time_threshold:
                    break
                if not message.text or is_processed(channel, message.id):
                    continue

                if check_relevance(message.text):
                    print(f"Совпадение в {channel}!")
                    formatted = format_message(message.text)
                    if formatted:
                        # Отправляем через нашу новую надежную функцию
                        send_telegram_message(MY_CHANNEL_ID, formatted)
                        print("Отправлено в канал.")
                    save_processed(channel, message.id)
        except Exception as e:
            print(f"Ошибка чтения {channel}: {e}")
        
        await asyncio.sleep(delay)

    print(f"[{datetime.now()}] Парсинг завершен.")

async def main():
    init_db()

    # Нам нужен только юзербот для чтения!
    userbot = Client("user", api_id=API_ID, api_hash=API_HASH, workdir="/app/data")

    schedule_mins = config['parsing']['schedule_minutes']
    scheduler = AsyncIOScheduler()
    # Убрали bot из аргументов
    scheduler.add_job(agent_job, 'interval', minutes=schedule_mins, args=[userbot])

    async with userbot:
        print("Юзербот запущен!")
        scheduler.start()
        await agent_job(userbot) # Первый запуск
        print(f"Агент работает. Проверка каждые {schedule_mins} мин.")
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())