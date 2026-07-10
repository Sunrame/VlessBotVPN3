FROM python:3.13-slim

# libpq5   — рантайм-библиотека (та самая libpq.so.5 из первой ошибки).
# libpq-dev — заголовки + pg_config, нужны ТОЛЬКО для сборки psycopg2 из
#             исходников (если под Python 3.13 ещё нет готового wheel).
# gcc      — компилятор, тоже нужен только для этой сборки.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Совпадает с командой из Procfile — Railway передаёт $PORT автоматически.
CMD gunicorn app:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
