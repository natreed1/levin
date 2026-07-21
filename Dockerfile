# Unified Workflow Messenger — build from the repository root:
#   fly deploy --config messenger/fly.toml --dockerfile Dockerfile
# Or: docker build -f Dockerfile -t workflow-messenger .

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app \
    MESSENGER_DATA_DIR=/data \
    MESSENGER_USERS_DIR=/data/users

COPY messenger/requirements.txt /tmp/messenger-requirements.txt
COPY pyproject.toml README.md /app/
COPY src /app/src
RUN pip install --no-cache-dir -r /tmp/messenger-requirements.txt \
    && pip install --no-cache-dir /app

COPY messenger /app/messenger

EXPOSE 8080

CMD ["sh", "-c", "uvicorn messenger.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
