FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    MESSENGER_DATA_DIR=/data

COPY requirements.txt /app/messenger/requirements.txt
RUN pip install --no-cache-dir -r /app/messenger/requirements.txt

COPY . /app/messenger

EXPOSE 8080

CMD ["sh", "-c", "uvicorn messenger.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
