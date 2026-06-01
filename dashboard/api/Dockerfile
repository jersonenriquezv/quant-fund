FROM python:3.12-slim

WORKDIR /app

COPY dashboard/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy package structure so "dashboard.api" imports work
COPY dashboard/__init__.py ./dashboard/__init__.py
COPY dashboard/api/ ./dashboard/api/

# Copy config (settings.py needed for API key auth, CORS config)
COPY config/__init__.py ./config/__init__.py
COPY config/settings.py ./config/settings.py

# Copy shared models + the pure SMC detector modules used by the chart
# detector-replay endpoint (/api/chart/detections). strategy_service/__init__.py
# is lazy, so this does NOT pull in service.py's heavy deps (anthropic/ccxt).
COPY shared/ ./shared/
COPY strategy_service/ ./strategy_service/

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["python", "-m", "uvicorn", "dashboard.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
