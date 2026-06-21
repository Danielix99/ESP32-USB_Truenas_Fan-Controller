FROM python:3.12-slim

RUN pip install --no-cache-dir pyserial websocket-client

WORKDIR /app
COPY fan_control.py /app/fan_control.py

CMD ["python", "fan_control.py"]
