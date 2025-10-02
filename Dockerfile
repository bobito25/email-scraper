# syntax=docker/dockerfile:1

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5004
EXPOSE 5004

CMD ["gunicorn", "web_app:app", "--bind", "0.0.0.0:5004", "--workers", "1", "--timeout", "120"]
