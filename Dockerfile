FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Almaty

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic/ ./alembic/
COPY backend/ ./backend/
COPY main.py .
COPY index.html .
COPY workspace-sync*.js ./
COPY preview-offline*.js ./
COPY offline-sw.js ./
COPY app/ ./app/

EXPOSE 8832

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8832/ || exit 1

CMD ["sh", "-c", "alembic upgrade head && python main.py"]

