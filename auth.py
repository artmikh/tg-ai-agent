import os
from pyrogram import Client

# Берем данные из переменных окружения
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

if not API_ID or not API_HASH:
    print("ОШИБКА: Укажите API_ID и API_HASH в файле .env")
    exit(1)

print("=== Авторизация в Telegram ===")
print("Введи номер телефона, привязанный к аккаунту, с которого будем читать каналы:")

# Создаем клиента, сессия сохранится в /app/data/user.session
app = Client(
    name="user", 
    api_id=API_ID, 
    api_hash=API_HASH, 
    workdir="/app/data"
)

app.run()
print("Авторизация успешна! Сессия сохранена. Теперь можно запускать агента.")