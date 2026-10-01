FROM python:3.11-slim AS builder
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --upgrade pip
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim AS release
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
WORKDIR /app
COPY app ./app
RUN mkdir -p /app/data
VOLUME /app/data
EXPOSE 8080
ENV PYTHONUNBUFFERED=True
ENV DATA_DIR=/app/data
CMD ["python", "app/main.py"]
