# 基于 Alpine 的实验版本 — 用于对比镜像体积，暂不引入 multi-stage
FROM python:3.13-alpine

# 运行时依赖
RUN apk add --no-cache \
        mpv \
        procps \
        pulseaudio-utils \
        alsa-utils \
        avahi avahi-tools \
        shairport-sync \
        mpd \
        upmpdcli \
        dbus \
        ffmpeg \
        curl \
        openssl

# 创建 shairport-sync 系统用户（Alpine 包没建，导致 DBus policy 报 unknown user 警告）
RUN adduser -D -H -s /sbin/nologin shairport-sync

WORKDIR /app

# 安装 yt-dlp + Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir yt-dlp \
    && pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY mediacenter/ ./mediacenter/
COPY config.yaml.example ./config.yaml.example
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 创建数据目录
RUN mkdir -p /etc/mediacenter /tmp/tts_cache \
    /var/run/dbus /var/run/avahi-daemon

# 默认配置
RUN cp config.yaml.example /etc/mediacenter/config.yaml

EXPOSE 8080 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["-c", "/etc/mediacenter/config.yaml"]
