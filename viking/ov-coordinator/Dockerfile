FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY coordinator.py .

EXPOSE 1933
CMD ["uvicorn", "coordinator:app", "--host", "0.0.0.0", "--port", "1933", "--log-level", "info"]
