# One image, every process. Which one a container runs is picked by its command
# (see Procfile and docker-compose.yml): gunicorn for the web process, one of the
# `run_*_jobs --loop` management commands for a worker, `migrate` for the release
# step. Nothing here is platform-specific.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Needs no database and no real SECRET_KEY: only collects what rest_framework
# and django.contrib.admin ship, into STATIC_ROOT, baked into the image.
RUN DEBUG=True python manage.py collectstatic --noinput

RUN useradd --create-home --uid 1000 schoolos && chown -R schoolos:schoolos /app
USER schoolos

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/', timeout=3)" || exit 1

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
