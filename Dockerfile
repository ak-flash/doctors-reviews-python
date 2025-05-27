FROM debian:stable-slim

SHELL ["/bin/bash", "-c"]

# Установка необходимых пакетов
RUN apt-get update && apt-get install -y xvfb python3 python3-pip python3-venv 

# Копирование кода приложения
WORKDIR /app
COPY requirements.txt .
COPY main.py .

# Создание директории для данных и настройка прав доступа
RUN mkdir -p /app/data

# Установка зависимостей Python
RUN python3 -m venv venv \
 && source venv/bin/activate \
 && pip3 install --no-cache-dir -r requirements.txt \
 && patchright install chrome

# Запуск приложения
CMD ["xvfb-run", "--server-args=-screen 0 1024x768x24", "/app/venv/bin/python", "/app/main.py"]