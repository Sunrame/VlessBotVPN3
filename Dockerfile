FROM python:3.12-slim

# Под Python 3.12 у psycopg2-binary есть готовый wheel — сборка из
# исходников вообще не должна запускаться. Но на случай если pip всё же
# решит собирать (другая архитектура/будущее обновление пакета) — оставляем
# полный набор инструментов сборки как страховку, чтобы это не всплыло
# четвёртым кругом:
# libpq5         — рантайм-библиотека (libpq.so.5).
# libpq-dev      — заголовки + pg_config для сборки psycopg2.
# build-essential — gcc, make, libc6-dev (assert.h и другие Си-заголовки).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 libpq-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Совпадает с командой из Procfile — Railway передаёт $PORT автоматически.
CMD gunicorn app:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
