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


## Установка на Ubuntu-сервер

Рекомендуемый способ — Docker Compose. Команды выполняются от пользователя с `sudo`.

### 1. Установить Docker

```bash
sudo apt update
sudo apt install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
```

Проверить установку:

```bash
sudo docker --version
sudo docker compose version
```

### 2. Скачать проект

```bash
sudo mkdir -p /opt/doctors-reviews
sudo chown "$USER":"$USER" /opt/doctors-reviews
git clone https://github.com/ak-flash/doctors-reviews-python /opt/doctors-reviews
cd /opt/doctors-reviews
```

Если каталог уже содержит проект:

```bash
cd /opt/doctors-reviews
git pull
```

### 3. Настроить `.env`

```bash
cp .env.example .env
nano .env
```

Минимальные настройки:

```dotenv
AI_API_URL=https://openrouter.ai/api/v1
AI_API_KEY=your-provider-api-key
AI_MODEL=provider/model-name
API_AUTH_ENABLED=true
API_KEY=change-this-api-key
CORS_ORIGINS=https://your-domain.example
```

`AI_API_URL`, `AI_API_KEY` и `AI_MODEL` должны относиться к одному OpenAI-compatible провайдеру. Не добавляйте `.env` в публичный репозиторий.

### 4. Запустить API

```bash
docker compose up -d --build --force-recreate --wait
docker compose ps
curl http://127.0.0.1:9000/health
```

Логи:

```bash
docker compose logs -f web
```

API будет доступен локально на `http://127.0.0.1:9000`. Документация: `/docs`.

### Обновление после изменений

```bash
cd /opt/doctors-reviews
git pull
docker compose up -d --build --force-recreate --wait
```

### Остановка

```bash
docker compose down
```

## Установка без Docker

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
cd /opt
git clone https://github.com/ak-flash/doctors-reviews-python doctors-reviews
cd doctors-reviews
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```

Сервис слушает `0.0.0.0:9000`. Для постоянной работы используйте systemd.

## Автозапуск через systemd

Создать `/etc/systemd/system/doctors-reviews.service`:

```ini
[Unit]
Description=Doctors Reviews API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/doctors-reviews
ExecStart=/opt/doctors-reviews/venv/bin/python /opt/doctors-reviews/main.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Замените `User=ubuntu`, если проект запускается от другого пользователя. Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now doctors-reviews
sudo systemctl status doctors-reviews
journalctl -u doctors-reviews -f
```

## Nginx reverse proxy

```bash
sudo apt install -y nginx
sudo nano /etc/nginx/sites-available/doctors-reviews
```

```nginx
server {
    listen 80;
    server_name your-domain.example;

    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 120s;
        proxy_read_timeout 300s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/doctors-reviews /etc/nginx/sites-enabled/doctors-reviews
sudo nginx -t
sudo systemctl reload nginx
```

Для HTTPS установите Certbot и выпустите сертификат для своего домена:

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your-domain.example
```

## Проверка sentiment API

```bash
curl -X POST http://127.0.0.1:9000/api/v1/checkSentiment \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: change-this-api-key' \
  -d '{"review":"Врач помог, лечение дало отличный результат"}'
```
