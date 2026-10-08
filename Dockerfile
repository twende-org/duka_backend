FROM python:3.12-slim

# Reel generation shells out to ffmpeg; slim images don't ship it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Directories the container writes to (collectstatic output, uploaded media).
# chown now so the runtime app user (created below) can write without a
# root-owned bind mount blocking it.
RUN mkdir -p /app/media /app/staticfiles /app/docker \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --shell /usr/sbin/nologin app \
    && chown -R app:app /app/media /app/staticfiles

USER app

EXPOSE 8000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["sh", "-c", "exec gunicorn --workers \"${WEB_CONCURRENCY:-3}\" --threads \"${WEB_THREADS:-2}\" --worker-tmp-dir /dev/shm --timeout 60 --access-logfile - --forwarded-allow-ips '*' --bind 0.0.0.0:8000 config.wsgi:application"]
