#!/bin/sh
# OpenWrt 媒体中心安装脚本
# 用法: sh scripts/install_openwrt.sh

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
echo "[3/7] 安装音频组件..."
opkg install mpv alsa-utils
# 可选: pulseaudio (如果需要更灵活的音频路由)
# opkg install pulseaudio-daemon pulseaudio-tools

# 4. 安装 AirPlay (shairport-sync)
echo "[4/7] 安装 AirPlay 支持 (shairport-sync)..."
opkg install shairport-sync

# 5. 安装 DLNA (gmrender-resurrect)
echo "[5/7] 安装 DLNA 支持..."
opkg install gmrender-resurrect

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
