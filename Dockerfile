# 基于 Alpine 的实验版本 — 用于对比镜像体积，暂不引入 multi-stage
#
# 基础镜像钉在 alpine3.23，不要改回不带小版本号的 python:3.13-alpine 或升到更新的 Alpine：
#
# Alpine 3.24 起打包的 shairport-sync 5.x 没有 PulseAudio 后端。shairport-sync 5.0 把
# configure 开关从 --with-pa / --with-pw 改名成 --with-pulseaudio / --with-pipewire，
# 而 Alpine 的 APKBUILD 仍传旧开关；autoconf 对认不出的 --with-xxx 只告警不报错，
# 于是 pa / pw 两个后端被静默丢掉。本应用在 audio.backend=pulse 下用 `-o pa` 启动
# shairport-sync（见 mediacenter/airplay/receiver.py 的 _build_command /
# _write_runtime_config），没有这个后端 AirPlay 根本起不来。
#
# 另外 5.x 还把该后端的注册名从 "pa" 改成了 "pulseaudio"（audio_pa.c 的 .name），且没有
# 别名。所以将来升到带 shairport-sync 5.x 的基础镜像时，即使 Alpine 已修好 APKBUILD，
# receiver.py 里传给 shairport-sync 的后端名也必须一起改，否则同样起不来。
#
# 升级前的判断标准就是下方 apk add 之后的构建期断言：`shairport-sync -h` 列出的
# "Available audio backends" 里必须有应用使用的后端名。断言失败 = 上述问题尚未解决。
FROM python:3.13-alpine3.23

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

# 构建期断言：shairport-sync 必须提供应用使用的 PulseAudio 后端名（原因见文件顶部说明）。
# 升级基础镜像或 shairport-sync 后端名时，这里和 receiver.py 要同步改。
RUN shairport-sync -h 2>&1 \
        | sed -n '/Available audio backends/,/^$/p' \
        | grep -qE '^[[:space:]]+pa( |$)' \
    || { echo "ERROR: shairport-sync 没有 'pa' 后端（见 Dockerfile 顶部说明）"; \
         shairport-sync -V 2>&1 || true; exit 1; }

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
