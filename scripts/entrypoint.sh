#!/bin/sh
# 容器入口脚本 — 启动 dbus → avahi → mediacenter（AirPlay 由应用内托管）

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

echo "[entrypoint] AirPlay 将由 mediacenter 进程托管启动..."

echo "[entrypoint] 启动 mediacenter..."
exec python3 -m mediacenter "$@"
