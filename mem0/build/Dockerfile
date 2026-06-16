FROM python:3.12-slim

WORKDIR /app

# libpq5 is required at runtime by psycopg (v3) — upstream's requirements.txt
# pins plain `psycopg` (not psycopg[binary]) and the slim base ships no libpq,
# so the stock server/Dockerfile produces an image that fails to import.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

ENV PYTHONUNBUFFERED=1

# Drop upstream's --reload (dev-only; restarts on file changes, needs watchfiles).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
