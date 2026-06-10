import os
import asyncio
import requests
from dotenv import load_dotenv
from pyrogram import Client

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MY_CHANNEL_ID = os.getenv("MY_CHANNEL_ID") # Оставляем как строку, для URL так лучше
TARGET_CHANNELS = [ch.strip() for ch in os.getenv("TARGET_CHANNELS", "").split(",") if ch.strip()]

# Функция для надежной отправки через чистый Bot API
def send_message_via_api(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload)
        result = response.json()
        if result.get("ok"):
            print("Успешно отправлено через Bot API!")
            return True
        else:
            print(f"Ошибка Bot API: {result.get('description')}")
            return False
    except Exception as e:
        print(f"Ошибка запроса: {e}")
        return False

async def main():
    # Клиент ТОЛЬКО для чтения (твоя сессия)
    userbot = Client("user", api_id=API_ID, api_hash=API_HASH, workdir="/app/data")

    messages_dict = {}

    async with userbot:
        print("\n=== ТЕСТИРОВАНИЕ ПАРСИНГА И ОТПРАВКИ ===")
        msg_counter = 1
        
        for channel in TARGET_CHANNELS:
            print(f"\n[Чтение канала: {channel}]")
            try:
                async for message in userbot.get_chat_history(channel, limit=5):
                    text = message.text or message.caption or "<Нет текста (только медиа)>"
                    messages_dict[msg_counter] = {"text": text, "channel": channel}
                    print(f"  {msg_counter}. {text[:80]}...")
                    msg_counter += 1
            except Exception as e:
                print(f"  Ошибка при чтении {channel}: {e}")
            
            await asyncio.sleep(2)

        if not messages_dict:
            print("\nСообщения не найдены.")
            return

        print("\n" + "="*40)
        while True:
            choice = input("Введите номер сообщения для отправки (или 'q' для выхода): ").strip()
            
            if choice.lower() == 'q':
                break
                
            try:
                choice_idx = int(choice)
                if choice_idx in messages_dict:
                    selected_text = messages_dict[choice_idx]["text"]
                    selected_channel = messages_dict[choice_idx]["channel"]
                    
                    print(f"\nОтправляю сообщение из '{selected_channel}' в {MY_CHANNEL_ID}...")
                    send_message_via_api(MY_CHANNEL_ID, f"✅ **Тест агента:**\n\n{selected_text}")
                    break
                else:
                    print("Такого номера нет.")
            except ValueError:
                print("Введите число или 'q'.")

if __name__ == "__main__":
    asyncio.run(main())