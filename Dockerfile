FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir --trusted-host pypi.python.org -r requirements.txt

COPY . .

EXPOSE 5000


ENV TARGET_URL="https://httpbin.org/get"
ENV ENABLE_URL_REWRITING="true"
ENV WORKERS=4 # Number of Gunicorn workers

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "$WORKERS", "proxy_app:app"]