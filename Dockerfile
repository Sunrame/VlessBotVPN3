FROM python:3.13-slim

# libpq5 — системная библиотека PostgreSQL-клиента (та самая libpq.so.5,
# которой не хватало в рантайме). Ставим её явно, не полагаясь на то, как
# конкретно Railway-билдер (Railpack/Nixpacks) соберёт рантайм-слой.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Совпадает с командой из Procfile — Railway передаёт $PORT автоматически.
CMD gunicorn app:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
