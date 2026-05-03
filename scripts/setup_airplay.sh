#!/bin/sh
# AirPlay (shairport-sync) 配置脚本
# 用法: sh scripts/setup_airplay.sh

set -e

AIRPLAY_NAME="${1:-OpenWrt MediaCenter}"
CONF="/etc/shairport-sync.conf"

echo "====================================="
echo "  配置 AirPlay (shairport-sync)"
echo "====================================="
echo ""
echo "设备名称: $AIRPLAY_NAME"
echo ""

# 检查是否已安装
if ! command -v shairport-sync >/dev/null 2>&1; then
    echo "错误: shairport-sync 未安装"
    echo "请先运行: opkg install shairport-sync"
    exit 1
fi

# 备份原配置
if [ -f "$CONF" ]; then
    cp "$CONF" "${CONF}.bak"
    echo "已备份原配置到 ${CONF}.bak"
fi

# 生成配置文件
cat > "$CONF" << EOF
// shairport-sync 配置文件
// 由 OpenWrt MediaCenter 生成

general = {
    // AirPlay 设备显示名称
    name = "$AIRPLAY_NAME";

    // 插值方式: "basic" 或 "soxr" (需安装 libsoxr)
    interpolation = "basic";

    // 密码 (留空则不需要密码)
    // password = "your_password";

    // 输出后端: "pa" (PulseAudio, 推荐) 或 "alsa"
    // PulseAudio 会自动路由到蓝牙音箱等当前默认设备
    output_backend = "pa";

    // 漂移容忍度 (毫秒)
    drift_tolerance_in_seconds = 0.002;

    // 重新同步阈值 (秒)
    resync_threshold_in_seconds = 0.050;
};

// PulseAudio 输出配置 (当 output_backend = "pa" 时)
// 不指定 sink 则自动使用系统默认 sink (蓝牙音箱等)
// pa = {
//     application_name = "shairport-sync";  // 在 PulseAudio 中显示的名称
//     sink = "";                             // 留空 = 使用默认 sink
// };

// 元数据配置 (用于检测播放状态)
metadata = {
    enabled = "yes";
    include_cover_art = "no";
    pipe_name = "/tmp/shairport-sync-metadata";
    pipe_timeout = 5000;
};

// 会话控制
sessioncontrol = {
    // 当有新连接时的行为
    // allow_session_interruption = "yes";

    // 会话超时 (秒)
    session_timeout = 120;
};

// 诊断
diagnostics = {
    // 日志详细程度: 0=最少, 3=最多
    log_verbosity = 1;
};
EOF

echo "配置文件已写入: $CONF"
echo ""

# 创建元数据管道
if [ ! -p /tmp/shairport-sync-metadata ]; then
    mkfifo /tmp/shairport-sync-metadata
    echo "已创建元数据管道: /tmp/shairport-sync-metadata"
fi

# 启用开机自启
if [ -f /etc/init.d/shairport-sync ]; then
    /etc/init.d/shairport-sync enable
    echo "已设置 shairport-sync 开机自启"
fi

# 重启服务
if [ -f /etc/init.d/shairport-sync ]; then
    /etc/init.d/shairport-sync restart
    echo "shairport-sync 已重启"
else
    echo "提示: 可手动启动: shairport-sync -c $CONF"
fi

echo ""
echo "====================================="
echo "  AirPlay 配置完成！"
echo "====================================="
echo ""
echo "现在可以在 iPhone/iPad/Mac 上看到 AirPlay 设备:"
echo "  设备名: $AIRPLAY_NAME"
echo ""
echo "测试方法:"
echo "  1. 打开 iPhone 控制中心"
echo "  2. 长按音乐播放区域"
echo "  3. 点击 AirPlay 图标"
echo "  4. 选择 '$AIRPLAY_NAME'"
echo ""
