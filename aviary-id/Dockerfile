# CUDA 12.6 on purpose. The target GPUs are Pascal cards (Quadro P1000, GTX 10xx —
# all sm_61) and CUDA 13.0 drops Pascal entirely, so this is the branch to stay on
# for this hardware.
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Keep every model download on the mounted volume rather than in the image layer.
    # torchvision's detector weights and the Hugging Face BioCLIP snapshot both land here.
    TORCH_HOME=/models/torch \
    HF_HOME=/models/huggingface \
    CACHE_DIR=/models \
    # Ultralytics writes its settings file here instead of $HOME, keeping the container
    # filesystem read-only-friendly and the config on the persistent volume.
    YOLO_CONFIG_DIR=/models/ultralytics \
    # Let the allocator grow and release segments rather than holding a fixed pool. On a
    # small card shared with other processes, fragmentation alone can turn a fit into an
    # out-of-memory error.
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

# libgl1/libglib2.0-0 are OpenCV's runtime libraries, pulled in by ultralytics for the
# YOLO detector backend.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# torch/torchvision MUST come from the cu126 index. PyTorch removed Maxwell/Pascal
# (sm_61) kernels from the cu128 and cu129 builds, so a default `pip install torch`
# produces a container that imports fine, reports torch.cuda.is_available() == True on
# some setups, and then fails at the first kernel launch — or silently runs on CPU.
# If you upgrade torch, verify the new version still publishes a +cu126 wheel.
RUN pip3 install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu126 \
        torch==2.8.0 torchvision==0.23.0

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY app /app/app
WORKDIR /app

EXPOSE 8100

# Model weights are NOT baked in — they download to /models on first start. Mount that
# as a volume or every container recreate re-downloads ~2 GB (~3.5 GB with
# TRAINED_CLASSIFIER=inat21).
VOLUME ["/models"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=600s --retries=3 \
    CMD python3 -c "import urllib.request,json,sys; \
sys.exit(0 if json.load(urllib.request.urlopen('http://localhost:8100/healthz'))['ok'] else 1)"

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
