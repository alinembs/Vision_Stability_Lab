FROM nvcr.io/nvidia/deepstream-l4t:7.1-triton-multiarch

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    python3-gi \
    python3-dev \
    gir1.2-gst-rtsp-server-1.0 \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-tools \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# pyds 1.2.0 (cp310/aarch64) — wheel oficial compatível com DeepStream 7.1.
# O release v1.2.2 só publica cp312; o build local v1.1.8 falha contra os
# headers do DS 7.1 (INC-003). Wheel baixado de:
# https://github.com/NVIDIA-AI-IOT/deepstream_python_apps/releases/download/v1.2.0/pyds-1.2.0-cp310-cp310-linux_aarch64.whl
COPY deploy/wheels/pyds-1.2.0-cp310-cp310-linux_aarch64.whl /tmp/
RUN pip3 install --no-cache-dir /tmp/pyds-1.2.0-cp310-cp310-linux_aarch64.whl \
    && rm /tmp/pyds-1.2.0-cp310-cp310-linux_aarch64.whl

COPY . /app

CMD ["python3", "deepstream/pipeline_real.py"]
