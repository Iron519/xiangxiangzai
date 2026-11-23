# syntax=docker/dockerfile:1
FROM python:3.12-slim

# 使用国内 Debian 镜像源加速 apt 下载
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources

# 安装 LibreOffice（用于 Office 转 PDF：Writer/Calc/Impress）+ 中文字体
# --mount=type=cache 让 apt 下载包缓存到宿主机，即使 --no-cache 也能复用 deb 文件
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-calc libreoffice-impress \
    fonts-wqy-zenhei

WORKDIR /app

# 先单独 COPY requirements.txt，利用 Docker 层缓存：改代码不会触发重装依赖
# --mount=type=cache 让 pip 下载缓存到宿主机，即使 --no-cache 也能复用 wheel 文件
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

# 本地 BGE embedding + reranker 依赖（CPU 版 torch，避免拉 CUDA 全家桶）
# 指定确切版本，避免 pip 依赖解析回溯下载大量历史版本
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 \
    --index-url https://download.pytorch.org/whl/cpu torch==2.15.0+cpu && \
    pip install --timeout 120 -i https://mirrors.aliyun.com/pypi/simple/ --no-deps \
    "transformers==4.46.0" "sentence-transformers==3.3.0" \
    "huggingface_hub==0.26.2" "tokenizers==0.20.3" \
    "datasets==2.21.0" sentencepiece==0.2.3 peft==0.22.0 accelerate==1.16.0 && \
    pip install --timeout 120 -i https://mirrors.aliyun.com/pypi/simple/ \
    safetensors scipy scikit-learn protobuf filelock sympy fsspec jinja2 mpmath MarkupSafe pyarrow lz4 dill multiprocess aiohttp attrs narwhals rich tqdm typer

CMD ["sleep", "infinity"]
