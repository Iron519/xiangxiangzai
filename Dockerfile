FROM python:3.12-slim

# 使用国内 Debian 镜像源加速 apt 下载
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources

# 安装 LibreOffice（用于 Office 转 PDF：Writer/Calc/Impress）+ 中文字体
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-calc libreoffice-impress \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先单独 COPY requirements.txt，利用 Docker 层缓存：改代码不会触发重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

CMD ["sleep", "infinity"]
