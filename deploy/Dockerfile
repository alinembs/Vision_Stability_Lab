# Base do DeepStream. O padrão é o que este host roda hoje (Orin NX: L4T
# R36.x / JetPack 6 / DeepStream 7.1). Para o Orin Nano (JetPack 7.2 / DS 9.1)
# o `make doctor` avisa quando a plataforma não bate e a imagem precisa ser
# trocada — ver `docs/plans/2026-11-01-portabilidade-orin-nano.md`.
ARG DS_BASE_IMAGE=nvcr.io/nvidia/deepstream-l4t:7.1-triton-multiarch
FROM ${DS_BASE_IMAGE}

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

# pyds: o wheel precisa casar com o **Python da imagem base** — cp310 no
# DeepStream 7.1 (L4T R36 / JetPack 6) e cp312 no JetPack 7 + DS 9.1. Fixar o
# arquivo (como antes) amarrava a imagem a uma única plataforma; agora o build
# escolhe o wheel de `deploy/wheels/` que casa com o interpretador e falha com
# mensagem clara quando não há nenhum — em vez de deixar um `import pyds`
# quebrado em runtime (classe do INC-003).
#
# Wheel oficial cp310 do DS 7.1 (único que funciona contra os headers do 7.1;
# ver INC-003):
# https://github.com/NVIDIA-AI-IOT/deepstream_python_apps/releases/download/v1.2.0/pyds-1.2.0-cp310-cp310-linux_aarch64.whl
# Para DS 9.1 (Python 3.12) o release v1.2.2 publica cp312.
COPY deploy/wheels/ /tmp/wheels/
RUN set -u; \
    tag="cp$(python3 -c 'import sys; print("%d%d" % sys.version_info[:2])')"; \
    whl="$(ls /tmp/wheels/pyds-*-"$tag"-*-linux_aarch64.whl 2>/dev/null | head -1 || true)"; \
    if [ -z "$whl" ]; then \
        echo "ERRO: nenhum wheel pyds para Python $tag em deploy/wheels/." >&2; \
        echo "Coloque o wheel que casa com a base ($tag) e refaça o build." >&2; \
        exit 1; \
    fi; \
    echo "pyds: usando $whl"; \
    pip3 install --no-cache-dir "$whl" && rm -rf /tmp/wheels

COPY . /app

CMD ["python3", "deepstream/pipeline_real.py"]
