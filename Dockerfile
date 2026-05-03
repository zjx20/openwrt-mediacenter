FROM python:3.11-slim

# 安装系统依赖（含 AirPlay / DLNA 全套组件）
RUN apt-get update && apt-get install -y --no-install-recommends \
    mpv \
    procps \
    pulseaudio-utils \
    alsa-utils \
    avahi-daemon avahi-utils libnss-mdns \
    shairport-sync \
    gmediarender \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-libav \
    gstreamer1.0-pulseaudio \
    dbus \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 yt-dlp
RUN pip install --no-cache-dir yt-dlp

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY mediacenter/ ./mediacenter/
COPY config.yaml.example ./config.yaml.example
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 创建数据目录
RUN mkdir -p /etc/mediacenter /etc/mediacenter/scripts /etc/mediacenter/job_logs \
    /tmp/tts_cache /tmp/news \
    /var/run/dbus /var/run/avahi-daemon

# 默认配置（运行时可挂载覆盖）
RUN cp config.yaml.example /etc/mediacenter/config.yaml

# API 端口 + AirPlay 端口
EXPOSE 8080 5000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["-c", "/etc/mediacenter/config.yaml"]
