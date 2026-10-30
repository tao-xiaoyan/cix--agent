FROM python:3.12-slim
WORKDIR /app
COPY cloud-platform/backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY shared /app/shared
COPY cloud-platform/backend /app/backend
ENV PYTHONPATH=/app:/app/backend PYTHONDONTWRITEBYTECODE=1
WORKDIR /app/backend
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8002 --workers 1 --no-access-log"]
