FROM python:3.12-slim

WORKDIR /app

# libgomp1 = OpenMP runtime required by lightgbm's native lib (engine1 meta-label
# model inference). Not present in python:3.12-slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, signal; os.kill(1, 0)" || exit 1

CMD ["python", "-u", "main.py"]
