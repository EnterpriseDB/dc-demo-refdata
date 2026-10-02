FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY webapp ./webapp
COPY usecase1_order_status ./usecase1_order_status
COPY usecase3_device_telemetry ./usecase3_device_telemetry

# Bind to all interfaces: required for the NorthFlank sandbox. The UI is
# unauthenticated, so only run this in an isolated sandbox (see README).
ENV HOST=0.0.0.0 \
    PORT=5050

# psycopg-binary bundles its own OpenSSL, whose default CA path is not Debian's,
# so sslrootcert=system finds no roots. Point it at the system bundle.
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

RUN useradd --system --no-create-home app
USER app

EXPOSE 5050

CMD ["python", "webapp/app.py"]
