FROM python:3.13-slim

# libpq5        — рантайм-библиотека (libpq.so.5, ошибка №1).
# libpq-dev     — заголовки + pg_config для сборки psycopg2 (ошибка №2).
# build-essential — полный набор для сборки Си (gcc, make и, что важно,
#                   libc6-dev — без него не находится даже assert.h, ошибка №3).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 libpq-dev build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Совпадает с командой из Procfile — Railway передаёт $PORT автоматически.
CMD gunicorn app:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
