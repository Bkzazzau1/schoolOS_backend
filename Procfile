web: gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 3
release: python manage.py migrate --noinput
worker_media: python manage.py run_media_jobs --loop
worker_mandates: python manage.py run_mandate_jobs --loop
worker_collection: python manage.py run_collection_jobs --loop
