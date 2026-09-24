## Doctors Reviews API

Описание:
Сервис для сбора отзывов с медицинских платформ СберЗдоровье (https://docdoc.ru), ПроДокторов (https://prodoctorov.ru) и анализа их тональности (положительная, отрицательная) с помощью ИИ.

Документация OpenAPI:
https://doc-reviews.ak-vps.ru/docs

Доступные роуты:

    GET /api/v1/getReviews?url=...&platform=...&all_reviews=... — получить отзывы
    POST /api/v1/checkSentiment — анализ сентиментальности отзыва (тональности: положительный, отрицательный) (JSON: {'{"review": "текст"}'})

Параметры:

    platform — поддерживаемые платформы:
        sberzdorovie — СберЗдоровье (https://sberhealth.ru, https://docdoc.ru)
        prodoctorov — ПроДокторов (https://prodoctorov.ru)
    url — ссылка на страницу врача на выбранной платформе.
    Примеры:
    https://docdoc.ru/doctor/ivanov-ivan
    https://prodoctorov.ru/moskva/vrach/ivanov_ivan/

Используемые модули:
    curl_cffi
    fastapi

Примечание: ранее для резервного браузерного режима использовался Camoufox. Он действительно работал, но для текущего сбора данных через HTML и API SberЗдоровья больше не нужен и удалён из рабочего окружения.
    
Настройки анализа сентиментальности отзыва (дополнительно):

    AI_API_URL=https://openrouter.ai/api/v1
    AI_MODEL=provider/model-name
    AI_API_KEY=your-provider-api-key

Используется OpenAI-compatible API. Можно указать любой провайдер с endpoint `chat/completions`: OpenRouter, OpenAI, Groq, Together, Ollama и другие.
`AI_API_URL` должен быть base URL API, обычно с `/v1`; если указать полный URL `/chat/completions`, приложение автоматически удалит этот суффикс.


## Установка

    sudo apt-get install python3 python3-pip python3-venv

## Create a virtual environment 
    python -m venv venv

## Activate the virtual environment

#### on Windows
    venv\Scripts\activate.bat

#### on macOS and Linux
    source venv/bin/activate

## Install packages
#### Install using requirements
    pip install -r requirements.txt

OR

#### Manual install
    pip install curl_cffi fastapi[standard] uvicorn

## Запуск скрипта

### 1. Обычный запуск (на всех платформах)
Скрипт запускает HTTP-сборщики без браузера:
    python main.py

## Обновление Docker-контейнера

После изменения кода или зависимостей выполните в корне проекта:

```powershell
docker compose up -d --build --force-recreate --wait
```

Проверить состояние контейнера:

```powershell
docker compose ps
docker compose logs -f web
```

Если контейнер не обновился, пересоздайте его полностью:

```powershell
docker compose down
docker compose up -d --build --force-recreate --wait
```

### 3. Если ошибки при запуске
sudo apt update
sudo apt install -y \
  libgtk-3-0t64 libnss3 libx11-6 libxcb1 libxcomposite1 libxcursor1 \
  libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 \
  libxshmfence1 libxtst6 fonts-liberation libasound2t64 libdrm2 libgbm1 \
  libatk1.0-0t64 libatk-bridge2.0-0t64 libpango-1.0-0 libcairo2 libxkbcommon0

sudo ldconfig
python main.py  

## Ubuntu автозагрузка

sudo nano /etc/systemd/system/doctors-reviews.service

[Unit]
Description=Doctors Reviews API Service
After=network.target
Wants=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/doctors-reviews
ExecStart=/home/ubuntu/doctors-reviews/venv/bin/python /home/ubuntu/doctors-reviews/main.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target

Примените изменения и включите сервис:
sudo systemctl daemon-reload
sudo systemctl enable doctors-reviews.service
sudo systemctl start doctors-reviews.service

Проверьте статус и логи:
systemctl status doctors-reviews.service
journalctl -u doctors-reviews.service -f   # логи в реальном времени

## nginx конфиг

location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        
        # Стандартные прокси-заголовки
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # Поддержка WebSocket (если понадобится)
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Таймауты для браузерных операций
        proxy_connect_timeout 60s;
        proxy_send_timeout 120s;
        proxy_read_timeout 300s;
    }