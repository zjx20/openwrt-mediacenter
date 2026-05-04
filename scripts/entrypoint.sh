#!/bin/sh
# 容器入口脚本 — 启动 dbus → avahi → mediacenter（AirPlay 由应用内托管）

set -e

echo "[entrypoint] 启动 dbus..."
rm -f /run/dbus/dbus.pid
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

# 配置 TTS ducking：role=tts 出现时，把 background / airplay / dlna 角色的音量
# 自动压低，TTS 结束后恢复。重复加载会失败但无害（pactl 会返回非零）。
DUCK_VOLUME="${TTS_DUCK_VOLUME:--20dB}"
if pactl info >/dev/null 2>&1; then
    if pactl list short modules 2>/dev/null | grep -q module-role-ducking; then
        echo "[entrypoint] PA module-role-ducking 已存在，跳过加载"
    else
        if pactl load-module module-role-ducking \
            trigger_roles=tts \
            ducking_roles=background,airplay,dlna \
            volume="$DUCK_VOLUME" \
            global=true >/dev/null 2>&1; then
            echo "[entrypoint] 已加载 PA module-role-ducking (volume=$DUCK_VOLUME)"
        else
            echo "[entrypoint] 加载 PA module-role-ducking 失败（宿主机 PA 可能已配置或不支持），忽略"
        fi
    fi
fi

echo "[entrypoint] AirPlay 将由 mediacenter 进程托管启动..."

echo "[entrypoint] 启动 mediacenter..."
exec python3 -m mediacenter "$@"
