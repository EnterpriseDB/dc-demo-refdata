FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common ./common
COPY webapp ./webapp
COPY usecase1_order_status ./usecase1_order_status
COPY usecase3_device_telemetry ./usecase3_device_telemetry

ENV HOST=0.0.0.0 \
    PORT=5050

EXPOSE 5050

CMD ["python", "webapp/app.py"]
