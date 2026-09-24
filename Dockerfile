FROM debian:stable-slim

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip python3-venv xvfb xauth libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdbus-1-3 libdrm2 libgtk-3-0 libgbm1 libasound2 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxshmfence1 libxkbcommon0 \
 && rm -rf /var/lib/apt/lists/*

ENV XDG_CACHE_HOME=/opt/browser-cache
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .

RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/data /opt/browser-cache /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix


RUN python3 -m venv venv \
 && venv/bin/pip install --no-cache-dir -r requirements.txt \
 && venv/bin/camoufox fetch \
 && chown -R appuser:appuser /app /opt/browser-cache

COPY --chown=appuser:appuser main.py sentiment_service.py index.html favicon.ico check_access.py .env.example ./
COPY --chown=appuser:appuser collectors ./collectors
COPY --chown=appuser:appuser app ./app
USER appuser
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["/app/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/health')"]
ENV DISPLAY=:99
CMD ["sh", "-c", "Xvfb :99 -screen 0 1365x900x24 -nolisten tcp & sleep 1; exec /app/venv/bin/python /app/main.py"]
