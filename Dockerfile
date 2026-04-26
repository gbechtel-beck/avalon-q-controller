FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_DATA_DIR=/data

WORKDIR /srv

# Install deps
COPY requirements.txt /srv/requirements.txt
RUN pip install -r requirements.txt

# Copy application
COPY app /srv/app

# Data volume for settings & event log
VOLUME ["/data"]
RUN mkdir -p /data

EXPOSE 8000

# Health probe: hit /api/miners (always returns 200 with empty list at minimum)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/miners', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
