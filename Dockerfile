FROM python:3.12-slim

# 安装 LibreOffice（用于 Office 转 PDF）+ 中文字体（精简版）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-calc \
    fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

CMD ["sleep", "infinity"]
