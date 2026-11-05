FROM python:3.13-slim

LABEL org.opencontainers.image.title="garmin-dashboard"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    HOME=/tmp \
    TZ=America/Chicago

# tzdata so TZ works; util-linux provides setpriv (dropping root); apply Debian security updates.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends tzdata util-linux \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock .
# Every package file is checked against the SHA-256 hashes in the lock file.
RUN pip install --require-hashes --no-deps -r requirements.lock
# pip isn't needed at runtime; removing it shrinks what's left inside the container.
RUN python -m pip uninstall -y pip

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN find /app -type d -exec chmod 755 {} + && find /app -type f -exec chmod 644 {} + \
 && chmod 755 /entrypoint.sh

# The app always runs as PUID:PGID (Unraid's nobody:users, 99:100). If the container
# starts as root, entrypoint.sh fixes the data folder's owner and drops to that user.
ENV PUID=99 PGID=100
VOLUME /data
EXPOSE 8099

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8099/healthz', timeout=4); sys.exit(0)"]

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099", "--no-server-header", "--no-proxy-headers"]
