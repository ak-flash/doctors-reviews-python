## Doctors Reviews API

Сервис собирает отзывы о врачах с медицинских платформ СберЗдоровье (https://sberhealth.ru, https://docdoc.ru) и ПроДокторов (https://prodoctorov.ru) и определяет их тональность (положительная, отрицательная) с помощью ИИ.

Документация OpenAPI: https://doc-reviews.ak-vps.ru/docs

### Роуты

    GET  /api/v1/getReviews?url=...&platform=...&all_reviews=... — получить отзывы
    POST /api/v1/checkSentiment — определить тональность отзыва, тело запроса: {"review": "текст"}
    GET  /health — проверка работоспособности
    GET  /metrics — счётчики запросов, ошибок, попаданий в кэш и запусков браузера

Параметры `getReviews`:

    platform — sberzdorovie (СберЗдоровье) или prodoctorov (ПроДокторов)
    url — ссылка на страницу врача на выбранной платформе, например:
        https://docdoc.ru/doctor/ivanov-ivan
        https://prodoctorov.ru/moskva/vrach/ivanov_ivan/
    all_reviews — true, чтобы собрать все отзывы, а не только показанные на странице врача
        (по умолчанию false; в ответе не больше MAX_RESULT_REVIEWS)

Если включена авторизация (`API_AUTH_ENABLED=true`), передавайте ключ в заголовке `X-API-Key`.

### Как собираются отзывы

Оба источника защищены антибот-системой ServicePipe: на запросы с серверных IP она отвечает страницей JS-проверки вместо страницы врача. Поэтому сбор идёт в два шага:

1. Обычный HTTP-запрос (httpx). Если источник отдал страницу врача, отзывы разбираются сразу.
2. Если пришла страница проверки или капча, страница открывается в браузере [Camoufox](https://camoufox.com) — Firefox с защитой от обнаружения автоматизации. Он проходит JS-проверку и сохраняет cookies в `BROWSER_PROFILE_DIR`. Первый запрос после запуска сервиса занимает около 10 секунд, следующие — несколько секунд.

Если с одного IP идёт много запросов подряд, ServicePipe вместо JS-проверки показывает капчу с картинкой, которую сервис пройти не может. Тогда API вернёт `503 source_blocked`. Снизьте частоту запросов (успешные ответы кэшируются на `CACHE_TTL_SECONDS`) или настройте прокси с другим IP (`HTTPS_PROXY`).

Проверить доступ к источникам с сервера:

```bash
python check_access.py                                        # без Docker
docker compose exec web /app/venv/bin/python check_access.py  # в Docker
```

Скрипт проверяет тестовые страницы `TEST_URL` и `PRODOCTOROV_TEST_URL`: сначала обычным HTTP-запросом, затем через браузер. С сервера HTTP-запрос обычно блокируется (`source_blocked`) — это нормально, главное, чтобы браузер вернул отзывы.

Основные библиотеки: FastAPI, httpx, Camoufox (Playwright), lxml, OpenAI SDK.

### Настройки

Все настройки задаются в `.env` (образец — `.env.example`).

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `AI_API_URL`, `AI_API_KEY`, `AI_MODEL` | — | OpenAI-совместимый провайдер для анализа тональности |
| `API_AUTH_ENABLED`, `API_KEY` | `false`, пусто | Проверка заголовка `X-API-Key` на всех роутах, кроме `/`, `/favicon.ico` и `/health` |
| `CORS_ORIGINS` | пусто | Разрешённые origin через запятую |
| `CACHE_TTL_SECONDS` | `900` | Сколько секунд кэшировать успешный ответ |
| `BLOCKED_CACHE_TTL_SECONDS` | `30` | Сколько секунд кэшировать ответ `source_blocked` |
| `CACHE_MAX_ENTRIES` | `256` | Размер каждого из кэшей |
| `MAX_RESULT_REVIEWS` | `200` | Максимум отзывов в ответе |
| `RATE_LIMIT`, `RATE_WINDOW_SECONDS` | `30`, `60` | Сколько запросов разрешено с одного IP и ключа за окно |
| `MAX_CONCURRENT_COLLECTIONS` | `10` | Сколько сборов выполняется одновременно |
| `SENTIMENT_MAX_BODY_BYTES` | `1048576` | Максимальный размер тела запроса `checkSentiment` |
| `BROWSER_HEADLESS` | `false` | Режим браузера: `false` — обычное окно (нужен дисплей), `virtual` — Camoufox сам запускает виртуальный дисплей Xvfb (только Linux, рекомендуется для сервера без Docker), `true` — headless |
| `BROWSER_PROFILE_DIR` | `data/browser` | Каталог профиля браузера с cookies |
| `BROWSER_TIMEOUT_SECONDS` | `60` | Таймаут сбора через браузер |
| `HTTP_PROXY`, `HTTPS_PROXY` | пусто | Прокси для запросов к источникам и для браузера |
| `TEST_URL`, `PRODOCTOROV_TEST_URL` | — | Тестовые страницы для `check_access.py` |

В Docker `BROWSER_HEADLESS` и `BROWSER_PROFILE_DIR` задаются в `docker-compose.yml`: браузер работает в обычном режиме на виртуальном дисплее Xvfb внутри контейнера.

Для анализа тональности используется OpenAI-compatible API. Можно указать любой провайдер с endpoint `chat/completions`: OpenRouter, OpenAI, Groq, Together, Ollama и другие.
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

Если источник блокирует IP сервера (API возвращает `source_blocked`, а `check_access.py` не получает отзывы и через браузер), задайте прокси:

```dotenv
HTTP_PROXY=http://user:password@proxy-host:proxy-port
HTTPS_PROXY=http://user:password@proxy-host:proxy-port
```

Страницы источников открываются по HTTPS, поэтому HTTP-запросы идут через `HTTPS_PROXY`; браузер использует `HTTPS_PROXY`, а если он не задан — `HTTP_PROXY`. Поддерживаются HTTP/HTTPS proxy URL и прокси с авторизацией. Не вставляйте пароль от прокси в публичные файлы.

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

Docker-образ уже содержит Camoufox и Xvfb, дополнительно ничего устанавливать не нужно. Проверить доступ к источникам:

```bash
docker compose exec web /app/venv/bin/python check_access.py
```

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
python -m camoufox fetch
sudo venv/bin/python -m playwright install-deps firefox
cp .env.example .env
nano .env
python main.py
```

`python -m camoufox fetch` скачивает браузер в `~/.cache/camoufox` текущего пользователя, а `playwright install-deps firefox` устанавливает системные библиотеки браузера и Xvfb. На сервере без графического окружения укажите в `.env`:

```dotenv
BROWSER_HEADLESS=virtual
```

Сервис слушает `0.0.0.0:9000`. Проверить доступ к источникам: `python check_access.py`. Для постоянной работы используйте systemd.

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

Замените `User=ubuntu`, если проект запускается от другого пользователя. Это должен быть тот же пользователь, от которого выполнялся `python -m camoufox fetch`: браузер ищется в его `~/.cache/camoufox`. Затем:

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

## Тесты

```bash
pytest
```

Нужен `AI_API_KEY` в `.env` или в окружении — для unit-тестов подойдёт любое значение. Тест с реальным запросом к ИИ-провайдеру: `pytest --live`.
