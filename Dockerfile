FROM python:3.12-slim

LABEL org.opencontainers.image.title="GoSync"
LABEL org.opencontainers.image.description="Self-hosted GoPro cloud media recovery from a browser HAR export"
LABEL org.opencontainers.image.source="https://github.com/PrabhanshuAttri/GoSync"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gosync.py .

VOLUME ["/data"]

ENTRYPOINT ["python", "/app/gosync.py"]
