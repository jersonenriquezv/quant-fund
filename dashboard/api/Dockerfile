FROM python:3.12-slim

WORKDIR /app

COPY dashboard/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy package structure so "dashboard.api" imports work
COPY dashboard/__init__.py ./dashboard/__init__.py
COPY dashboard/api/ ./dashboard/api/

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["python", "-m", "uvicorn", "dashboard.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
