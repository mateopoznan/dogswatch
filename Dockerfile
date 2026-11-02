FROM python:3.13-slim
ARG YAMNET_URL=https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite
ARG YAMNET_SHA256=4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf
ENV TZ=Europe/Warsaw PYTHONUNBUFFERED=1
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg tzdata curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Google YAMNet audio model (Apache 2.0), checked against a known checksum.
RUN curl -fsSL "$YAMNET_URL" -o yamnet.tflite && echo "$YAMNET_SHA256  yamnet.tflite" | sha256sum -c -
COPY app.py i18n.py ./
COPY templates ./templates
COPY static ./static
RUN mkdir -p /app/data
EXPOSE 1717
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:1717/static/app.css', timeout=4)"
CMD ["python", "app.py"]
