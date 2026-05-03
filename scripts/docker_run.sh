#!/bin/sh
# Docker 构建与运行脚本
# 用法: sh scripts/docker_run.sh [build|run|run-host|stop|logs|setup-macvlan]

set -e

IMAGE_NAME="openwrt-mediacenter"
CONTAINER_NAME="mediacenter"
CONFIG_PATH="${CONFIG_PATH:-$(pwd)/config.yaml}"

# ---- macvlan 默认参数（按实际网络环境修改） ----
MACVLAN_NET="${MACVLAN_NET:-mcvlan}"
PARENT_IF="${PARENT_IF:-br-lan}"       # OpenWrt 一般是 br-lan，普通 Linux 常为 eth0
SUBNET="${SUBNET:-192.168.1.0/24}"
GATEWAY="${GATEWAY:-192.168.1.1}"
CONTAINER_IP="${CONTAINER_IP:-192.168.1.200}"

case "${1:-run}" in
  build)
    echo "=== 构建 Docker 镜像 ==="
    docker build -t "$IMAGE_NAME" .
    echo "构建完成: $IMAGE_NAME"
    ;;

  setup-macvlan)
    echo "=== 创建 macvlan 网络 ==="
    echo "  父接口: $PARENT_IF"
    echo "  子网:   $SUBNET"
    echo "  网关:   $GATEWAY"
    docker network create -d macvlan \
      --subnet="$SUBNET" \
      --gateway="$GATEWAY" \
      -o parent="$PARENT_IF" \
      "$MACVLAN_NET" || echo "(网络已存在，跳过)"
    echo "macvlan 网络 '$MACVLAN_NET' 就绪"
    ;;

  run)
    # macvlan 方式（推荐）
    if [ ! -f "$CONFIG_PATH" ]; then
      cp config.yaml.example "$CONFIG_PATH"
      echo "已创建配置文件: $CONFIG_PATH (请根据需要修改)"
    fi

    # 确保 macvlan 网络存在
    docker network inspect "$MACVLAN_NET" >/dev/null 2>&1 || {
      echo "macvlan 网络（$MACVLAN_NET）不存在，先运行: sh $0 setup-macvlan"
      exit 1
    }

    echo "=== 启动媒体中心容器 (macvlan, IP=$CONTAINER_IP) ==="
    docker run -d \
      --name "$CONTAINER_NAME" \
      --restart unless-stopped \
      --network "$MACVLAN_NET" \
      --ip "$CONTAINER_IP" \
      -e PULSE_SERVER=unix:/run/pulse/native \
      -v /run/pulse:/run/pulse \
      -v "$CONFIG_PATH":/etc/mediacenter/config.yaml:ro \
      -v /tmp/tts_cache:/tmp/tts_cache \
      -v /tmp/news:/tmp/news \
      -e AIRPLAY_NAME="${AIRPLAY_NAME:-OpenWrt MediaCenter}" \
      "$IMAGE_NAME"

    echo "容器已启动: $CONTAINER_NAME"
    echo "API 地址: http://$CONTAINER_IP:8080"
    echo "AirPlay 名称: ${AIRPLAY_NAME:-OpenWrt MediaCenter}"
    ;;

  run-host)
    # host network 备选方式
    if [ ! -f "$CONFIG_PATH" ]; then
      cp config.yaml.example "$CONFIG_PATH"
      echo "已创建配置文件: $CONFIG_PATH (请根据需要修改)"
    fi

    echo "=== 启动媒体中心容器 (host network) ==="
    docker run -d \
      --name "$CONTAINER_NAME" \
      --restart unless-stopped \
      --network host \
      -e PULSE_SERVER=unix:/run/pulse/native \
      -v /run/pulse:/run/pulse \
      -v "$CONFIG_PATH":/etc/mediacenter/config.yaml:ro \
      -v /tmp/tts_cache:/tmp/tts_cache \
      -v /tmp/news:/tmp/news \
      -e AIRPLAY_NAME="${AIRPLAY_NAME:-OpenWrt MediaCenter}" \
      "$IMAGE_NAME"

    echo "容器已启动: $CONTAINER_NAME"
    echo "API 地址: http://localhost:8080"
    ;;

  stop)
    echo "=== 停止容器 ==="
    docker stop "$CONTAINER_NAME" 2>/dev/null || true
    docker rm "$CONTAINER_NAME" 2>/dev/null || true
    echo "已停止"
    ;;

  logs)
    docker logs -f "$CONTAINER_NAME"
    ;;

  *)
    echo "用法: $0 {build|setup-macvlan|run|run-host|stop|logs}"
    exit 1
    ;;
esac
