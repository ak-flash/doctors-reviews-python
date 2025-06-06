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
    patchright
    fastapi
    
Настройки анализ сентиментальности отзыва (дополнительно):

    AI_API_URL: https://openrouter.ai/api/v1
    AI_MODEL: mistralai/mistral-small-24b-instruct-2501:free
    AI_API_KEY: sk-o...


# Установка

sudo apt-get install xvfb python3 python3-pip python3-venv 

## Create a virtual environment 
python -m venv venv

## Activate the virtual environment

###  on Windows
venv\Scripts\activate.bat

### on macOS and Linux
source venv/bin/activate

## Install using requirements
pip install -r requirements.txt

## Manual install
pip install patchright fastapi[standard]



## Install chrome browser
patchright install chrome

## Run script in virtual monitor
xvfb-run --server-args="-screen 0 1024x768x24" /home/ubuntu/doctors-reviews/venv/bin/python main.py