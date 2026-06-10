FROM python:3.11-slim

# Устанавливаем системные зависимости для tgcrypto
RUN apt-get update && apt-get install -y gcc libffi-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем зависимости и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем ВСЕ необходимые файлы проекта
COPY auth.py .
COPY agent.py .
COPY test_tg.py .
COPY config.yaml .

# Команда запуска по умолчанию (для постоянной работы агента)
CMD ["python", "agent.py"]