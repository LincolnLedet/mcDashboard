FROM docker.io/library/python:3.13.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py logsync.py ./
COPY templates templates

RUN useradd --uid 1000 --no-create-home mcdash && mkdir -p /app/data && chown mcdash /app/data
ENV HOME=/tmp
USER mcdash

EXPOSE 8081
# One worker keeps the status cache and rate limiter in a single process; threads handle concurrency.
CMD ["gunicorn", "--bind", "0.0.0.0:8081", "--workers", "1", "--threads", "8", "--access-logfile", "-", "app:app"]
