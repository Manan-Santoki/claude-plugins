# Build from the repository root (see docker-compose.yml) so the plugin's push client can be bundled.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server/s3up_server ./s3up_server
COPY plugins/s3-upload/skills/s3-upload/scripts/s3up.py ./s3up_server/s3up.py

RUN useradd --uid 10001 --create-home s3up && mkdir -p /data && chown s3up /data
USER s3up
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["uvicorn", "s3up_server.app:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
