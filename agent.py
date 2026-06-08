import os
import asyncio
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import OpenAI

# Загружаем переменные из .env
load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MY_CHANNEL_ID = os.getenv("MY_CHANNEL_ID")
TARGET_CHANNELS = [ch.strip() for ch in os.getenv("TARGET_CHANNELS", "").split(",")]
KEYWORDS = [kw.strip() for kw in os.getenv("KEYWORDS", "").split(",")]

# Инициализируем клиента OpenAI
ai_client = OpenAI(api_key=OPENAI_API_KEY)

# --- Настройка базы данных SQLite ---
DB_PATH = "/app/data/agent_db.sqlite" # Путь внутри докера

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS processed_messages (
            channel_id TEXT,
            message_id INTEGER,
            PRIMARY KEY (channel_id, message_id)
        )
    ''')
    conn.commit()
    conn.close()

def is_message_processed(channel_id, message_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM processed_messages WHERE channel_id=? AND message_id=?', (str(channel_id), message_id))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def save_message(channel_id, message_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO processed_messages (channel_id, message_id) VALUES (?, ?)', (str(channel_id), message_id))
    conn.commit()
    conn.close()

# --- ИИ Функции ---
def check_relevance(text):
    """Проверяет, подходит ли сообщение под ключевые слова (с учетом синонимов и опечаток)"""
    prompt = f"""
    Ты — анализатор текста. Определи, относится ли текст сообщения к следующим темам или ключевым словам: {', '.join(KEYWORDS)}.
    Учитывай синонимы, опечатки, сленг и аббревиатуры (например, "свч" = "микроволновка").
    Ответь ТОЛЬКО одним словом: YES (если относится) или NO (если не относится).
    Текст: {text}
    """
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        answer = response.choices[0].message.content.strip().upper()
        return answer == "YES"
    except Exception as e:
        print(f"Ошибка OpenAI при фильтрации: {e}")
        return False

def format_message(text):
    """Форматирует сообщение по шаблону"""
    prompt = f"""
    Ты — редактор. Извлеки из текста суть и оформи её строго по шаблону.
    Если каких-то данных в тексте нет, пиши "Не указано".
    
    Шаблон:
    🔥 **Найдено предложение!**
    📦 **Товар/Услуга:** [Название]
    💰 **Цена:** [Цена]
    📍 **Локация/Контакт:** [Контакты]
    📝 **Детали:** [Краткое описание своими словами, 1-2 предложения]

    Текст для обработки:
    {text}
    """
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"Ошибка OpenAI при форматировании: {e}")
        return None

# --- Главная логика Агента ---
async def agent_job(userbot: Client):
    print(f"[{datetime.now()}] Запуск задачи агента...")
    
    # Смотрим сообщения за последний час (чтобы не читать всю историю)
    time_threshold = datetime.now() - timedelta(hours=1)

    for channel in TARGET_CHANNELS:
        print(f"Парсинг канала: {channel}")
        try:
            # Получаем последние 50 сообщений
            async for message in userbot.get_chat_history(channel, limit=50):
                if message.date.replace(tzinfo=None) < time_threshold:
                    break # Сообщения старше часа пропускаем
                
                if not message.text:
                    continue # Пропускаем картинки/видео без текста

                # Проверяем, обрабатывали ли уже
                if is_message_processed(channel, message.id):
                    continue

                # 1. Фильтруем через ИИ
                if check_relevance(message.text):
                    print(f"Найдено релевантное сообщение в {channel}: {message.text[:50]}...")
                    
                    # 2. Форматируем через ИИ
                    formatted_text = format_message(message.text)
                    if formatted_text:
                        # 3. Отправляем в наш канал (от имени Бота)
                        await userbot.send_message(
                            chat_id=MY_CHANNEL_ID,
                            text=formatted_text,
                            parse_mode="Markdown"
                        )
                        print("Сообщение успешно отправлено!")
                    
                    # Сохраняем ID в базу, чтобы не обработать повторно
                    save_message(channel, message.id)

        except Exception as e:
            print(f"Ошибка при парсинге канала {channel}: {e}")
        
        # ПАУЗА 5 СЕКУНД между каналами!
        print("Пауза 5 секунд...")
        await asyncio.sleep(5)

    print(f"[{datetime.now()}] Задача агента завершена.")

# --- Точка входа ---
async def main():
    init_db() # Инициализируем БД

    # Создаем клиента от имени пользователя (для чтения)
    # Сессия будет сохраняться в /app/data/user.session
    userbot = Client(
        name="user", 
        api_id=API_ID, 
        api_hash=API_HASH,
        workdir="/app/data"
    )

    # Настраиваем планировщик (каждый час)
    scheduler = AsyncIOScheduler()
    # Передаем объект userbot в функцию задачи
    scheduler.add_job(agent_job, 'interval', hours=1, args=[userbot])

    async with userbot:
        print("Юзербот запущен!")
        scheduler.start()
        print("Планировщик запущен. Агент работает.")
        
        # Запускаем задачу сразу при старте (чтобы не ждать час)
        await agent_job(userbot)

        # Держим скрипт работающим бесконечно
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())