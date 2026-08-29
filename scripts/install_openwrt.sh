#!/bin/sh
# OpenWrt 媒体中心安装脚本（直装方式，只覆盖官方软件源里有的部分）
# 用法: sh scripts/install_openwrt.sh
#
# 官方源的两个缺口需要自行补齐，否则请改用 Docker 部署（见 README）：
#   - 没有 mpv 包：背景音乐 / TTS 通道依赖它
#   - shairport-sync-* 不带 PulseAudio 后端（--with-pa）和 MPRIS 接口：
#     audio.backend 为 pulse 时需要自行编译的 shairport-sync

set -e

echo "====================================="
echo "  OpenWrt 媒体中心 安装脚本"
echo "====================================="

# 1. 更新软件源
echo "[1/7] 更新软件源..."
opkg update

# 2. 安装 Python3
echo "[2/7] 安装 Python3..."
opkg install python3 python3-pip python3-asyncio python3-logging

# 3. 安装音频相关
#    PulseAudio 不是可选项：mpv / shairport-sync 默认都走 pulse 输出，
#    DLNA 的 mpd 输出固定为 pulse，TTS ducking 也靠 PulseAudio 的 module-role-ducking
echo "[3/7] 安装音频组件..."
opkg install alsa-utils pulseaudio-daemon pulseaudio-tools
if ! command -v mpv >/dev/null 2>&1; then
    echo "警告: 官方源没有 mpv，请自行编译或从第三方源安装，否则背景音乐 / TTS 无法工作"
fi

# 4. 安装 AirPlay (shairport-sync + avahi mDNS)
#    官方 shairport-sync-openssl 只有 alsa 后端；audio.backend 为 pulse 时需换成自行编译的版本
echo "[4/7] 安装 AirPlay 支持 (shairport-sync + avahi)..."
opkg install shairport-sync-openssl avahi-dbus-daemon
/etc/init.d/avahi-daemon enable
/etc/init.d/avahi-daemon start

# 5. 安装 DLNA (mpd + upmpdcli；mpd-mini 缺 pulse 输出 / curl 输入插件，必须 mpd-full)
echo "[5/7] 安装 DLNA 支持 (mpd-full + upmpdcli)..."
opkg install mpd-full upmpdcli

# shairport-sync / mpd / upmpdcli 三个进程都由 mediacenter 自己拉起并托管，
# 包自带的 init.d 服务（opkg 装完会自动 enable）必须关掉，否则会抢端口和设备名
for svc in shairport-sync mpd upmpdcli; do
    if [ -f "/etc/init.d/$svc" ]; then
        /etc/init.d/$svc disable 2>/dev/null || true
        /etc/init.d/$svc stop 2>/dev/null || true
    fi
done

# 6. 安装 yt-dlp
echo "[6/7] 安装 yt-dlp..."
pip3 install yt-dlp

# 7. 安装 Python 依赖
echo "[7/7] 安装 Python 依赖..."
cd "$(dirname "$0")/.."
pip3 install -r requirements.txt

# 创建配置目录
mkdir -p /etc/mediacenter

# 复制默认配置
if [ ! -f /etc/mediacenter/config.yaml ]; then
    cp config.yaml.example /etc/mediacenter/config.yaml
    echo "已创建配置文件: /etc/mediacenter/config.yaml"
    echo "请根据需要编辑配置文件"
fi

echo ""
echo "====================================="
echo "  安装完成！"
echo "====================================="
echo ""
echo "后续步骤:"
echo "  1. 编辑配置: vi /etc/mediacenter/config.yaml"
echo "  2. 启动服务: python3 -m mediacenter -c /etc/mediacenter/config.yaml"
echo ""
