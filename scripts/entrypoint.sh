#!/bin/sh
# 容器入口脚本 — 启动 dbus → avahi → shairport-sync → mediacenter

set -e

echo "[entrypoint] 启动 dbus..."
rm -f /var/run/dbus/pid
dbus-daemon --system --nofork &
sleep 0.5

echo "[entrypoint] 启动 avahi-daemon..."
# 允许在容器中运行（禁用 chroot）
sed -i 's/rlimit-nproc=3/#rlimit-nproc=3/' /etc/avahi/avahi-daemon.conf 2>/dev/null || true
avahi-daemon --no-chroot --daemonize
sleep 0.5

# 等待宿主机 PulseAudio 就绪
echo "[entrypoint] 检查 PulseAudio 连接..."
for i in $(seq 1 10); do
    if pactl info >/dev/null 2>&1; then
        echo "[entrypoint] PulseAudio 已连接: $(pactl info 2>/dev/null | grep 'Default Sink')"
        break
    fi
    echo "[entrypoint] 等待 PulseAudio... ($i/10)"
    sleep 1
done

echo "[entrypoint] 启动 shairport-sync..."
AIRPLAY_NAME="${AIRPLAY_NAME:-OpenWrt MediaCenter}"

if [ -f /etc/mediacenter/shairport-sync.conf ]; then
    shairport-sync -c /etc/mediacenter/shairport-sync.conf &
else
    # 使用 PulseAudio 后端，音频自动路由到默认 sink（蓝牙音箱等）
    shairport-sync -a "$AIRPLAY_NAME" -o pa \
        --metadata-pipename /tmp/shairport-sync-metadata -v &
fi
sleep 0.5

echo "[entrypoint] 启动 mediacenter..."
exec python3 -m mediacenter "$@"
