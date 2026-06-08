FROM python:3.11-slim

# Устанавливаем системные зависимости для tgcrypto
RUN apt-get update && apt-get install -y gcc libffi-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код агента
COPY agent.py .

# Команда запуска
CMD ["python", "agent.py"]