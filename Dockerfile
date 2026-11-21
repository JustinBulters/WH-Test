FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WH_DATABASE=/data/wh.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates templates
COPY static static

# Run as a normal user. /data holds the database and session key and must be a volume.
RUN useradd --uid 1000 --create-home wh && mkdir /data && chown wh:wh /data
USER wh
VOLUME /data
EXPOSE 8000

CMD ["sh", "-c", "flask --app app init-db && exec gunicorn app:app --workers 2 --bind 0.0.0.0:8000 --forwarded-allow-ips '*'"]
