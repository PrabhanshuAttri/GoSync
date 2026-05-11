FROM python:3.12-slim

ARG GOSYNC_VERSION=1.1.1

LABEL org.opencontainers.image.title="GoSync"
LABEL org.opencontainers.image.description="Self-hosted GoPro Cloud media download from a browser HAR export"
LABEL org.opencontainers.image.source="https://github.com/PrabhanshuAttri/GoSync"
LABEL org.opencontainers.image.version="${GOSYNC_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/data
ENV PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["python", "-m", "gosync"]
