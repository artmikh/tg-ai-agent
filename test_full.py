import os
import asyncio
import json
import yaml
import re
from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.enums import ParseMode
from openai import OpenAI

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Умное определение ID
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
        return answer == "YES"
    except Exception as e:
        print(f"    [ИИ Фильтр] Ошибка: {e}")
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
            print("    [ИИ Формат] Неожиданный формат JSON")
            return []
            
        template = config['output_template']
        results = []
        
        for item in items:
            # Добавляем имя канала-источника
            item["source_channel"] = source_channel
            
            # ВАЖНО: Экранируем фигурные скобки в значениях ИИ, чтобы не ломался .format()
            safe_item = {}
            for key, value in item.items():
                if isinstance(value, str):
                    # Заменяем { на {{ и } на }}, чтобы Python воспринимал их как текст
                    safe_value = value.replace("{", "{{").replace("}", "}}")
                    safe_item[key] = safe_value
                else:
                    safe_item[key] = value
            
            # Теперь безопасно форматировать
            formatted_text = template.format(**safe_item)
            results.append(formatted_text)
            
        return results
        
    except Exception as e:
        print(f"    [ИИ Формат] Ошибка: {e}")
        return []

# --- Главная логика ---
async def main():
    userbot = Client("user", api_id=API_ID, api_hash=API_HASH, workdir="/app/data")
    bot = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir="/app/data")

    async with userbot, bot:
        print("\n=== ПОЛНЫЙ ТЕСТ АГЕНТА (Мульти-экстракция НКТ) ===")

        # Считываем лимит из config.yaml
        limit = config['parsing']['messages_per_channel']
        
        for channel in TARGET_CHANNELS:
            print(f"\n[Чтение канала: {channel}] (лимит сообщений: {limit})")
            try:
                # Используем переменную limit
                async for message in userbot.get_chat_history(channel, limit=limit):
                        # ВАЖНО: Берем и текст, и подпись к фото/видео
                        text = message.text or message.caption
                        
                        if not text:
                            print(f"  [ID:{message.id}] Пропуск: нет текста или подписи")
                            continue
                        
                        print(f"\n  Анализирую сообщение (ID:{message.id}): {text[:60]}...")
                        
                        # ШАГ 1: Локальный фильтр
                        print("  -> Проверка локальным фильтром...")
                        if not local_keyword_filter(text, KEYWORDS):
                            print("  ❌ Локальный фильтр: нет нужных слов. Пропускаем.")
                            continue
                        
                        print("  ✅ Локальный фильтр пройден! Отправляю в ИИ...")
                        
                        # ШАГ 2: ИИ-фильтр
                        is_relevant = check_relevance(text)
                        
                        if is_relevant:
                            print(f"  ✅ Найдено релевантное сообщение (ID:{message.id})!")
                            
                            # ШАГ 3: Форматирование (передаем имя канала)
                            formatted_messages = format_message(text, f"@{channel}")
                            
                            if formatted_messages:
                                # Отправляем КАЖДУЮ найденную позицию отдельным сообщением
                                for msg_text in formatted_messages:
                                    print(f"  -> Отправка позиции в канал...")
                                    try:
                                        await bot.send_message(
                                            chat_id=MY_CHANNEL_ID,
                                            text=msg_text,
                                            parse_mode=ParseMode.HTML
                                        )
                                        print("  🎉 Успешно отправлено!")
                                        await asyncio.sleep(1) 
                                    except Exception as send_err:
                                        print(f"  ❌ ОШИБКА ОТПРАВКИ: {send_err}")
                            else:
                                print("  ❌ ИИ не смог извлечь позиции НКТ из текста.")
                        else:
                            print("  ❌ ИИ-фильтр: не соответствует смыслу. Пропускаем.")
                        
                        await asyncio.sleep(2)
                    
            except Exception as e:
                print(f"  Ошибка при чтении канала {channel}: {e}")
            
            await asyncio.sleep(5)

        print("\n=== ТЕСТ ЗАВЕРШЕН ===")

if __name__ == "__main__":
    asyncio.run(main())