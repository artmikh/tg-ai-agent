import os
import asyncio
import sqlite3
import json
import yaml
import re
import hashlib  # Для хэширования текста и поиска дублей
import csv
import pickle
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import OpenAI

load_dotenv()

GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

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
        print("ОШИБКА: MY_CHANNEL_ID имеет неверный формат.")
        exit(1)

TARGET_CHANNELS = [ch.strip() for ch in os.getenv("TARGET_CHANNELS", "").split(",") if ch.strip()]
KEYWORDS = [kw.strip() for kw in os.getenv("KEYWORDS", "").split(",") if kw.strip()]

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ai_client = OpenAI(api_key=OPENAI_API_KEY)

# --- БАЗА ДАННЫХ ---
DB_PATH = "/app/data/agent_db.sqlite"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    # Новая таблица, заточенная под ETL и будущую выгрузку
    conn.execute('''CREATE TABLE IF NOT EXISTS found_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_channel TEXT,
                    message_id INTEGER,
                    text_hash TEXT,
                    raw_text TEXT,
                    status TEXT DEFAULT 'NEW',
                    formatted_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )''')
    conn.commit()
    conn.close()

def get_text_hash(text):
    """Очищает текст от мусора и создает MD5 хэш для поиска дублей"""
    # Убираем эмодзи, знаки препинания, переводим в нижний регистр
    clean_text = re.sub(r'[^\w\s]', '', text, flags=re.UNICODE).lower().strip()
    # Убираем лишние пробелы
    clean_text = re.sub(r'\s+', ' ', clean_text)
    return hashlib.md5(clean_text.encode('utf-8')).hexdigest()

def save_new_message(channel, message_id, text, text_hash):
    """Сохраняет новое сообщение или помечает как дубль"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute('SELECT id FROM found_messages WHERE source_channel=? AND message_id=?', (channel, message_id))
    if cur.fetchone():
        conn.close()
        return
        
    cur = conn.execute('SELECT id FROM found_messages WHERE text_hash=? AND status != "DUPLICATE"', (text_hash,))
    exists = cur.fetchone()
    
    if exists:
        conn.execute('INSERT INTO found_messages (source_channel, message_id, text_hash, raw_text, status) VALUES (?, ?, ?, ?, ?)',
                     (channel, message_id, text_hash, text, 'DUPLICATE'))
        conn.commit()
        conn.close()
        print(f"    ⚠️ Найден кросс-канальный дубль (оригинал БД ID {exists[0]}). Пропуск ИИ.")
    else:
        conn.execute('INSERT INTO found_messages (source_channel, message_id, text_hash, raw_text, status) VALUES (?, ?, ?, ?, ?)',
                     (channel, message_id, text_hash, text, 'NEW'))
        conn.commit()
        conn.close()
        print(f"    ➕ Уникальное сообщение сохранено в базу.")

def get_new_messages():
    """Берет сообщения для обработки ИИ"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute('SELECT id, raw_text, source_channel, message_id FROM found_messages WHERE status = "NEW"')
    rows = cur.fetchall()
    conn.close()
    return rows

def update_message_status(db_id, status, formatted_text=None):
    """Обновляет статус после обработки ИИ"""
    conn = sqlite3.connect(DB_PATH)
    if formatted_text:
        conn.execute('UPDATE found_messages SET status=?, formatted_text=? WHERE id=?', (status, formatted_text, db_id))
    else:
        conn.execute('UPDATE found_messages SET status=? WHERE id=?', (status, db_id))
    conn.commit()
    conn.close()

def upload_file_to_drive(filename, filepath):
    """Загружает локальный файл на Google Drive через OAuth (от имени пользователя)"""
    if not GOOGLE_DRIVE_FOLDER_ID:
        print("  [GDrive] Пропуск: не настроен ID папки")
        return False

    creds = None
    token_path = '/app/data/token.pickle'
    credentials_path = '/app/credentials.json'
    
    if not os.path.exists(credentials_path):
         print("  [GDrive] Пропуск: нет файла credentials.json")
         return False

    # Загружаем токен, если он есть
    if os.path.exists(token_path):
        with open(token_path, 'rb') as token:
            creds = pickle.load(token)

    # Если токена нет или он просрочен
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Сохраняем обновленный токен
                with open(token_path, 'wb') as token:
                    pickle.dump(creds, token)
            except Exception as e:
                print(f"  ❌ [GDrive] Ошибка обновления токена: {e}")
                return False
        else:
            print("  ❌ [GDrive] Ошибка: Токен не найден или недействителен. Сгенерируйте token.pickle локально!")
            return False

    try:
        service = build('drive', 'v3', credentials=creds)

        file_metadata = {
            'name': filename,
            'parents': [GOOGLE_DRIVE_FOLDER_ID]
        }
        media = MediaFileUpload(filepath, mimetype='text/csv')
        file = service.files().create(
            body=file_metadata, media_body=media, fields='id'
        ).execute()
        
        print(f"  📁 Файл {filename} успешно загружен на Google Drive!")
        return True

    except Exception as e:
        print(f"  ❌ Ошибка загрузки на Google Drive: {e}")
        return False

def save_and_upload_report(processed_data):
    """Создает CSV локально и отправляет на Диск"""
    if not processed_data:
        print("\n[Отчет] Нет новых отправленных сообщений для формирования файла.")
        return

    # Генерируем имя файла с текущей датой и временем
    now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"nkt_report_{now_str}.csv"
    filepath = f"/app/data/{filename}" # Сохраняем во временный Docker Volume

    try:
        # Записываем CSV
        with open(filepath, mode="w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            # Заголовки
            writer.writerow(["ID", "Канал", "Ссылка", "Дата", "Оригинал", "Отформатировано"])
            # Данные
            for row in processed_data:
                writer.writerow(row)
        
        print(f"\n[Отчет] Локальный файл {filename} создан. Отправка на Диск...")
        
        # Загружаем на Диск
        if upload_file_to_drive(filename, filepath):
            # Если успешно - удаляем локальный файл, чтобы не копить мусор
            os.remove(filepath)
            
    except Exception as e:
        print(f"  ❌ Ошибка создания CSV: {e}")

# --- ЛОКАЛЬНЫЙ ФИЛЬТР ---
def local_keyword_filter(text, keywords):
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
        answer = resp.choices[0].message.content.strip().upper()
        
        # ВАЖНО: Если ИИ вместо YES вернул JSON, значит он уже проверил текст и нашел там НКТ!
        # Считаем это за "YES"
        if "{" in answer and "}" in answer:
            print(f"    [ИИ Фильтр] ИИ вернул JSON вместо YES. Считаем как релевантное!")
            return True
            
        # Стандартная проверка
        if "YES" in answer:
            return True
            
        print(f"    [ИИ Фильтр] Сырой ответ: {answer}")
        return False
        
    except Exception as e:
        print(f"    OpenAI Filter Error: {e}")
        return False

def format_message(text, source_link):
    prompt = config['prompts']['formatter'].format(text=text)
    try:
        resp = ai_client.chat.completions.create(
            model=config['ai']['model'],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        raw_text = resp.choices[0].message.content.strip()
        
        # ВАЖНО: Выводим то, что реально ответил ИИ!
        print(f"    [ИИ Формат] Сырой ответ: {raw_text}")
        
        if raw_text.startswith("```json"): raw_text = raw_text[7:]
        if raw_text.startswith("```"): raw_text = raw_text[3:]
        if raw_text.endswith("```"): raw_text = raw_text[:-3]
            
        data = json.loads(raw_text.strip())
        if isinstance(data, dict): items = [data]
        elif isinstance(data, list): items = data
        else: return []
        
        # Если ИИ вернул пустой список (не нашел НКТ в тексте)
        if not items:
            print("    [ИИ Формат] ИИ вернул пустой список. Нет позиций НКТ.")
            return []
            
        template = config['output_template']
        results = []
        for item in items:
            item["source_link"] = source_link
            safe_item = {}
            for key, value in item.items():
                if isinstance(value, str):
                    safe_item[key] = value.replace("{", "{{").replace("}", "}}")
                else:
                    safe_item[key] = value
            results.append(template.format(**safe_item))
        return results
    except json.JSONDecodeError as e:
        print(f"    [ИИ Формат] Ошибка парсинга JSON: {e}")
        print(f"    [ИИ Формат] Ответ был: {raw_text}")
        return []
    except Exception as e:
        print(f"    [ИИ Формат] Общая ошибка: {e}")
        return []

# --- АГЕНТ ---
async def collect_job(userbot: Client):
    """ЭТАП 1: Сбор данных из каналов"""
    print(f"\n[{datetime.now()}] 📡 ЗАПУСК СБОРЩИКА...")
    scan_hours = config['parsing'].get('scan_depth_hours', 24)
    time_threshold = datetime.now() - timedelta(hours=scan_hours)
    limit = config['parsing']['messages_per_channel']
    delay = config['parsing']['delay_between_channels']

    for channel in TARGET_CHANNELS:
        print(f"Чтение: {channel} (лимит: {limit})")
        try:
            async for message in userbot.get_chat_history(channel, limit=limit):
                if message.date.replace(tzinfo=None) < time_threshold:
                    print(f"    [Время] Пропуск ID {message.id}: сообщение старое.")
                    continue
                
                text = message.text or message.caption
                if not text:
                    continue

                # Локальный фильтр
                if not local_keyword_filter(text, KEYWORDS):
                    print(f"    [Локальный фильтр] Пропуск ID {message.id}: нет ключевых слов.")
                    continue
                
                # Вычисляем хэш и сохраняем в базу (БЕЗ ссылки)
                text_hash = get_text_hash(text)
                save_new_message(channel, message.id, text, text_hash)
                
        except Exception as e:
            print(f"Ошибка чтения {channel}: {e}")
        await asyncio.sleep(delay)
    print(f"[{datetime.now()}] 📡 Сбор завершен.")


async def process_job(bot: Client):
    """ЭТАП 2: Обработка ИИ и публикация"""
    print(f"\n[{datetime.now()}] 🧠 ЗАПУСК ОБРАБОТЧИКА ИИ...")
    new_messages = get_new_messages()
    
    if not new_messages:
        print("Нет новых сообщений для ИИ-обработки.")
        return

    # Список для сбора данных, которые ушли в канал (для выгрузки в CSV)
    successfully_posted_data = []

    for db_id, raw_text, source_channel, message_id in new_messages:
        print(f"  Обработка записи БД #{db_id} из {source_channel}...")
        
        # ... (код генерации ссылки остается как был) ...
        if source_channel.startswith('@'):
            channel_slug = source_channel[1:]
            source_link = f"https://t.me/{channel_slug}/{message_id}"
        elif not str(source_channel).startswith('-'):
            source_link = f"https://t.me/{source_channel}/{message_id}"
        else:
            clean_chat_id = str(source_channel)[4:] if str(source_channel).startswith("-100") else str(source_channel)[1:]
            source_link = f"https://t.me/c/{clean_chat_id}/{message_id}"

        is_relevant = check_relevance(raw_text)
        if not is_relevant:
            print(f"    ❌ ИИ-фильтр: отклонено.")
            update_message_status(db_id, 'REJECTED')
            continue
        
        print(f"    🎯 ИИ-фильтр пройден! Форматирование...")
        formatted_messages = format_message(raw_text, source_link) 
        
        if formatted_messages:
            send_success = False
            for msg_text in formatted_messages:
                try:
                    await bot.send_message(
                        chat_id=MY_CHANNEL_ID,
                        text=msg_text,
                        parse_mode=ParseMode.HTML
                    )
                    print("    🚀 Успешно отправлено в канал.")
                    send_success = True
                    await asyncio.sleep(1)
                except Exception as send_err:
                    print(f"    ❌ Ошибка отправки: {send_err}")
            
            if send_success:
                full_formatted = "\n\n---\n\n".join(formatted_messages)
                update_message_status(db_id, 'POSTED', full_formatted)
                
                # ДОБАВЛЕНО: Собираем данные для CSV
                row = [
                    db_id, 
                    source_channel, 
                    source_link, 
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    (raw_text or "")[:500] + "..." if len(raw_text or "") > 500 else raw_text,
                    (full_formatted or "")[:1000] + "..." if len(full_formatted or "") > 1000 else full_formatted
                ]
                successfully_posted_data.append(row)
            else:
                update_message_status(db_id, 'SEND_ERROR')
        else:
            print("    ⚠️ ИИ не смог извлечь данные.")
            update_message_status(db_id, 'FORMAT_ERROR')

    print(f"[{datetime.now()}] 🧠 Обработка завершена.")
    
    # ДОБАВЛЕНО: Вызываем сохранение и выгрузку CSV
    save_and_upload_report(successfully_posted_data)


async def full_agent_job(userbot: Client, bot: Client):
    """Главная функция, запускаемая по расписанию"""
    await collect_job(userbot)
    await process_job(bot)
    schedule_mins = config['parsing']['schedule_minutes']
    print(f"\n[{datetime.now()}] Цикл завершен. Следующий запуск через {schedule_mins} мин.")


async def main():
    init_db()
    userbot = Client("user", api_id=API_ID, api_hash=API_HASH, workdir="/app/data")
    bot = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir="/app/data")

    schedule_mins = config['parsing']['schedule_minutes']
    scheduler = AsyncIOScheduler()
    scheduler.add_job(full_agent_job, 'interval', minutes=schedule_mins, args=[userbot, bot])

    async with userbot, bot:
        print("Клиенты запущены!")
        scheduler.start()
        await full_agent_job(userbot, bot)
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())