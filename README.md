# OpenWrt 媒体中心 (MediaCenter)

基于 Python 的 OpenWrt 多功能音频中心，支持**多优先级音频管理**、**AirPlay**、**DLNA**、**TTS 语音播报**、**定时任务**和 **AI 智能控制**。

## 功能特性

| 功能 | 说明 | 状态 |
|------|------|------|
| 🎵 背景音乐 | 支持 YouTube、B站、网易云、直接 URL 等 | ✅ |
| 📱 AirPlay | iPhone/iPad/Mac 无线推送音频 | ✅ |
| 📺 DLNA | Android/PC DLNA 推送音频 | ✅ |
| 🔊 TTS | 文字转语音播报 (edge-tts/OpenAI) | ✅ |
| ⏰ 定时任务 | 定时下载新闻联播等 | ✅ |
| 🤖 AI 代理 | GPT 驱动的智能控制 | ✅ |
| 🔀 优先级管理 | 自动暂停/恢复，TTS > AirPlay > 背景音乐 | ✅ |

### 音频优先级体系

```
优先级高 ▲  TTS 播报 (priority=2)
         │   ├── 播放时自动暂停所有低优先级
         │   └── 播完自动恢复
         │
         │  AirPlay / DLNA 推流 (priority=1)
         │   ├── 播放时自动暂停背景音乐
         │   └── 停止后自动恢复背景音乐
         │
优先级低 ▼  背景音乐 (priority=0)
              └── 持续循环播放，被打断后自动恢复
```

## 快速开始

### 1. 安装依赖 (OpenWrt)

```bash
# 一键安装
sh scripts/install_openwrt.sh

# 或手动安装
opkg update
opkg install python3 python3-pip mpv alsa-utils shairport-sync gmrender-resurrect
pip3 install -r requirements.txt
```

### 2. 配置

```bash
# 复制示例配置
cp config.yaml.example config.yaml

# 编辑配置
vi config.yaml
```

推荐显式设置以下音量项，避免 AirPlay / DLNA 接入时沿用不合适的历史音量：

```yaml
audio:
  default_volume: 50   # 仅作用于背景音乐和 TTS

airplay:
  default_volume: 80   # AirPlay 首次开始播放时的本地默认音量

dlna:
  default_volume: 40   # DLNA 串流结束回到待机后会恢复到这个值
```

默认行为说明：

- `audio.default_volume` 只影响项目内部的 mpv 播放通道（背景音乐、TTS）。
- `airplay.default_volume` 会在 AirPlay 开始播放时应用为本地输出默认值，之后仍可继续用手机端音量覆盖。
- `dlna.default_volume` 会在服务启动时预设给 MPD，并在每次 DLNA 串流结束后恢复，保证下一次投放有稳定起点。

建议按下面顺序验证：

1. 重启服务后，用 iPhone 首次连接 AirPlay，确认不必先把手机音量推到 100% 才能得到正常响度。
2. AirPlay 播放中在手机上调高或调低一次音量，确认仍能正常同步到音箱输出。
3. 停止 AirPlay，切换到 DLNA 首次投放，确认默认音量先落在 40% 左右，而不是沿用上一次偏大的值。
4. 在 DLNA 控制端再次调整音量，确认新音量可以覆盖默认值；停止后重新投放，默认值会再次回到 40%。

### 3. 启动

```bash
# 前台运行
python3 -m mediacenter -c config.yaml

# 带详细日志
python3 -m mediacenter -c config.yaml -v

# 指定端口
python3 -m mediacenter -c config.yaml -p 9090
```

### 4. Docker 部署 (推荐)

镜像内置了 shairport-sync (AirPlay)、gmediarender (DLNA)、avahi-daemon (mDNS) 等所有组件，无需在宿主机额外安装。

```bash
# 构建镜像
sh scripts/docker_run.sh build

# 创建 macvlan 网络 (首次)
sh scripts/docker_run.sh setup-macvlan

# 启动容器 (macvlan 方式，推荐)
sh scripts/docker_run.sh run

# 查看日志
sh scripts/docker_run.sh logs

# 停止
sh scripts/docker_run.sh stop
```

[`scripts/docker_run.sh`](scripts/docker_run.sh) 默认会准备宿主机数据目录 `/opt/mediacenter`，并将整个目录挂载到容器内 `/etc/mediacenter`。其中 `config.yaml` 仍通过独立只读挂载覆盖，其他运行数据（如 `user_jobs.json`、`scripts/`、`job_logs/`）统一落在该目录下。这样可以避免把 `user_jobs.json` 单文件 bind mount 到容器后，在更新任务状态时因原子替换触发 `Resource busy`。

#### 网络模式选择

容器需要让 iPhone/Mac 通过 mDNS 发现 AirPlay 设备。有两种方式：

**方式一：macvlan（推荐）**

macvlan 让容器获得局域网中的一个**独立 IP**，就像一台真实设备。mDNS 广播、AirPlay 发现、DLNA 发现全部开箱即用，且不占用宿主机端口。

```bash
# 1. 创建 macvlan 网络（只需一次）
#    根据你的网络环境修改参数：
#      PARENT_IF  — 宿主机上联网口 (OpenWrt 通常是 br-lan)
#      SUBNET     — 局域网网段
#      GATEWAY    — 网关地址
#      CONTAINER_IP — 给容器分配的固定 IP (确保不在 DHCP 范围内)
PARENT_IF=br-lan SUBNET=192.168.1.0/24 GATEWAY=192.168.1.1 \
  sh scripts/docker_run.sh setup-macvlan

# 2. 启动容器
CONTAINER_IP=192.168.1.200 AIRPLAY_NAME="客厅音箱" \
  sh scripts/docker_run.sh run

# 容器会得到 192.168.1.200 这个独立 IP
# API: http://192.168.1.200:8080
# AirPlay 名称: 客厅音箱
```

或者手动运行：

```bash
DATA_PATH=/opt/mediacenter
mkdir -p "$DATA_PATH/scripts" "$DATA_PATH/job_logs"
[ -e "$DATA_PATH/user_jobs.json" ] || echo "[]" > "$DATA_PATH/user_jobs.json"

docker network create -d macvlan \
  --subnet=192.168.1.0/24 \
  --gateway=192.168.1.1 \
  -o parent=br-lan \
  mcvlan

docker run -d \
  --name mediacenter \
  --restart unless-stopped \
  --network mcvlan \
  --ip 192.168.1.200 \
  -e PULSE_SERVER=unix:/run/pulse/native \
  -v /run/pulse:/run/pulse \
  -v "$DATA_PATH":/etc/mediacenter \
  -v $(pwd)/config.yaml:/etc/mediacenter/config.yaml:ro \
  -e AIRPLAY_NAME="客厅音箱" \
  openwrt-mediacenter
```

> **注意：macvlan 容器与宿主机之间默认无法互通。** 如需从宿主机访问容器 API，需要在宿主机上创建 macvlan 子接口：
> ```bash
> ip link add mcvlan-host link br-lan type macvlan mode bridge
> ip addr add 192.168.1.201/32 dev mcvlan-host
> ip link set mcvlan-host up
> ip route add 192.168.1.200/32 dev mcvlan-host  # 容器 IP
> ```

**方式二：host network（备选）**

如果不想折腾 macvlan，`--network host` 也能工作。容器直接使用宿主机网络栈，mDNS 广播同样没问题。缺点是端口和宿主机共享。

```bash
sh scripts/docker_run.sh run-host
```

或手动：

```bash
DATA_PATH=/opt/mediacenter
mkdir -p "$DATA_PATH/scripts" "$DATA_PATH/job_logs"
[ -e "$DATA_PATH/user_jobs.json" ] || echo "[]" > "$DATA_PATH/user_jobs.json"

docker run -d \
  --name mediacenter \
  --restart unless-stopped \
  --network host \
  -e PULSE_SERVER=unix:/run/pulse/native \
  -v /run/pulse:/run/pulse \
  -v "$DATA_PATH":/etc/mediacenter \
  -v $(pwd)/config.yaml:/etc/mediacenter/config.yaml:ro \
  -e AIRPLAY_NAME="客厅音箱" \
  openwrt-mediacenter
```

> **定时任务持久化说明：** 建议始终把宿主机的整个数据目录挂载到容器内 `/etc/mediacenter`，而不是分别挂载 `user_jobs.json`、`scripts/`、`job_logs/`。如果你之前使用旧版脚本，并且这三个路径本来就在同一个宿主机目录（默认就是 `/opt/mediacenter`），升级后通常只需要停止旧容器并用新版脚本重建即可，无需额外迁移数据；如果你是手写 `docker run`，请同步改成整目录挂载，否则用户任务更新 `last_run`、`last_status` 时仍可能遇到 `Resource busy`。

> ⚠️ 不要使用默认的 bridge 网络 — bridge 无法转发 mDNS 多播，iPhone 将发现不了 AirPlay 设备。

#### 容器中使用音频设备

本项目使用 **PulseAudio** 作为音频后端。PulseAudio 会自动将音频路由到当前默认输出设备（包括蓝牙音箱），无需手动指定设备。

**挂载宿主机 PulseAudio socket（推荐）**

容器通过 Unix socket 连接宿主机的 PulseAudio 服务，所有音频自动输出到宿主机当前的默认 sink（蓝牙音箱、USB 声卡等）：

```bash
docker run -d \
  --name mediacenter \
  -e PULSE_SERVER=unix:/run/pulse/native \
  -v /run/pulse:/run/pulse \
  -v /tmp/tts_cache:/tmp/tts_cache \
  openwrt-mediacenter
```

> 蓝牙音箱连接/断开时，PulseAudio 会自动切换默认 sink，容器内无需任何改动。

**如果宿主机 PulseAudio 以用户模式运行**（如普通 Linux 桌面）：

```bash
docker run -d \
  -e PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native \
  -v /run/user/$(id -u)/pulse:/run/pulse \
  --name mediacenter \
  openwrt-mediacenter
```

**备选：直接映射 ALSA 设备**（无 PulseAudio 时，config.yaml 中改 `backend: "alsa"`）：

```bash
docker run -d \
  --device /dev/snd \
  -v /dev/snd:/dev/snd \
  --name mediacenter \
  openwrt-mediacenter
```

#### 确认音频设备工作

```bash
# 宿主机：查看 PulseAudio sink（蓝牙音箱会出现在这里）
pactl list short sinks

# 宿主机：查看当前默认 sink
pactl info | grep "Default Sink"

# 容器内：测试 PulseAudio 连接
docker exec mediacenter pactl info

# 容器内：播放测试音
docker exec mediacenter paplay /usr/share/sounds/alsa/Front_Center.wav
```

#### OpenWrt Docker 特别注意

OpenWrt 上运行 Docker 需确保：

1. **内核支持**: 确认固件编译时包含了 `kmod-sound-core` 和你的声卡/蓝牙驱动
2. **PulseAudio**: 宿主机需运行 PulseAudio（OpenWrt: `opkg install pulseaudio-daemon pulseaudio-tools`）
3. **蓝牙音箱**: 宿主机通过 PulseAudio 连接蓝牙音箱后，容器内自动可用
4. **macvlan 父接口**: OpenWrt 默认网桥是 `br-lan`；如果你用的是单独的以太网口（如 `eth0`），需要相应修改 `PARENT_IF`

```bash
# OpenWrt 安装 Docker + PulseAudio
opkg install dockerd docker pulseaudio-daemon pulseaudio-tools

# 确认 PulseAudio 运行且蓝牙音箱已连接
pactl list short sinks

# 然后正常构建运行
docker build -t openwrt-mediacenter .
sh scripts/docker_run.sh setup-macvlan
sh scripts/docker_run.sh run
```

### 5. 注册为系统服务 (非 Docker 方式)

```bash
# 将项目复制到 /opt/mediacenter
cp -r . /opt/mediacenter

# 安装 init.d 服务
cp scripts/init.d_mediacenter /etc/init.d/mediacenter
chmod +x /etc/init.d/mediacenter

# 启用开机自启
/etc/init.d/mediacenter enable

# 启动
/etc/init.d/mediacenter start
```

---

## API 使用指南

启动后默认监听 `http://0.0.0.0:8080`，以下是所有 API 接口。

### 系统状态

```bash
# 健康检查
curl http://localhost:8080/api/health

# 完整状态
curl http://localhost:8080/api/status
```

### 背景音乐

```bash
# 播放 URL（支持 YouTube、B站等 yt-dlp 支持的所有平台）
curl -X POST http://localhost:8080/api/music/play \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}'

# 播放本地文件
curl -X POST http://localhost:8080/api/music/play \
  -H "Content-Type: application/json" \
  -d '{"url": "/music/song.mp3"}'

# 加载播放列表
curl -X POST http://localhost:8080/api/music/playlist \
  -H "Content-Type: application/json" \
  -d '{
    "urls": [
      "https://www.youtube.com/watch?v=xxx",
      "https://music.163.com/#/song?id=xxx",
      "/music/local.mp3"
    ],
    "play_mode": "shuffle"
  }'

# 搜索音乐
curl -X POST http://localhost:8080/api/music/search \
  -H "Content-Type: application/json" \
  -d '{"query": "周杰伦 晴天", "source": "youtube"}'

# 播放控制
curl -X POST http://localhost:8080/api/music/pause
curl -X POST http://localhost:8080/api/music/resume
curl -X POST http://localhost:8080/api/music/stop
curl -X POST http://localhost:8080/api/music/next
curl -X POST http://localhost:8080/api/music/prev

# 获取播放状态
curl http://localhost:8080/api/music/status
```

**播放模式 (play_mode):**
- `sequential` - 顺序播放，播完停止
- `shuffle` - 随机播放
- `repeat_one` - 单曲循环
- `repeat_all` - 列表循环

### TTS 语音播报

```bash
# 文字转语音并播放（会自动暂停背景音乐，播完自动恢复）
curl -X POST http://localhost:8080/api/tts/speak \
  -H "Content-Type: application/json" \
  -d '{"text": "你好，欢迎回家！现在是晚上七点。"}'

# 仅合成不播放
curl -X POST http://localhost:8080/api/tts/synthesize \
  -H "Content-Type: application/json" \
  -d '{"text": "测试语音"}'

# 清除 TTS 缓存
curl -X POST http://localhost:8080/api/tts/clear-cache
```

### 音量控制

```bash
# 设置背景音乐音量
curl -X POST http://localhost:8080/api/volume \
  -H "Content-Type: application/json" \
  -d '{"volume": 60, "channel": "background"}'

# 设置 TTS 音量
curl -X POST http://localhost:8080/api/volume \
  -H "Content-Type: application/json" \
  -d '{"volume": 80, "channel": "tts"}'
```

### AirPlay / DLNA 状态

```bash
curl http://localhost:8080/api/airplay/status
curl http://localhost:8080/api/dlna/status
```

### 定时任务

```bash
# 手动触发下载新闻联播
curl -X POST http://localhost:8080/api/scheduler/run/download_news
```

### AI 对话

```bash
# 需要先在 config.yaml 中配置 AI
curl -X POST http://localhost:8080/api/ai/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "播放一首轻音乐"}'

curl -X POST http://localhost:8080/api/ai/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "把音量调到 30，然后用语音告诉我现在几点"}'
```

---

## AirPlay 配置详解

AirPlay 让你可以从 iPhone、iPad 或 Mac 无线推送音频到 OpenWrt 设备。

### Docker 部署 (已内置)

如果使用 Docker 部署，shairport-sync 和 avahi-daemon 已内置在镜像中，无需额外安装。其中 avahi-daemon 会在容器启动时启动，AirPlay 的 `shairport-sync` 进程则由 `mediacenter` 应用内统一托管启动。你只需要：

1. 确保使用 **macvlan** 或 **host network**（见上方 Docker 部署章节）
2. 通过环境变量自定义 AirPlay 名称：
   ```bash
   docker run -d -e AIRPLAY_NAME="我的音箱" ...
   ```
3. 如需自定义 shairport-sync 配置，挂载配置文件：
   ```bash
   docker run -d \
     -v /path/to/shairport-sync.conf:/etc/mediacenter/shairport-sync.conf:ro \
     ...
   ```

### 非 Docker 部署 (手动安装)

如果直接在 OpenWrt 上运行（不用 Docker），需要手动安装：

```bash
# 1. 安装 shairport-sync 和 avahi
opkg update
opkg install shairport-sync avahi-daemon

# 2. 运行配置脚本
sh scripts/setup_airplay.sh "我的音箱"
#                            └── AirPlay 显示的名称

# 或者手动配置 (见下方)
```

### 手动配置 shairport-sync

非 Docker 直装时，编辑 `/etc/shairport-sync.conf`；如果是 Docker 并想自定义配置，请挂载到 `/etc/mediacenter/shairport-sync.conf`：

```
general = {
    name = "我的音箱";          // AirPlay 设备名称
    output_backend = "pa";      // 使用 PulseAudio 输出（自动路由到蓝牙音箱）
};

// 不需要指定具体设备，PulseAudio 自动路由到默认 sink

metadata = {
    enabled = "yes";                              // 启用元数据
    pipe_name = "/tmp/shairport-sync-metadata";   // 元数据管道
};
```

### 查看音频设备

```bash
# 列出所有 PulseAudio sink（蓝牙音箱会显示在这里）
pactl list short sinks

# 示例输出:
# 0  alsa_output.usb-xxx     module-alsa-card.c  s16le 2ch 44100Hz  RUNNING
# 1  bluez_sink.XX_XX_XX     module-bluez5-device.c  s16le 2ch 44100Hz  RUNNING

# 查看当前默认 sink
pactl info | grep "Default Sink"

# 手动切换默认输出到蓝牙音箱
pactl set-default-sink bluez_sink.XX_XX_XX_XX_XX_XX
```

### 启动 AirPlay

### 启动 AirPlay (非 Docker)

```bash
# 启用 avahi (设备发现服务)
/etc/init.d/avahi-daemon enable
/etc/init.d/avahi-daemon start

# 启用 shairport-sync
/etc/init.d/shairport-sync enable
/etc/init.d/shairport-sync start
```

### 在 iPhone/iPad 上使用

1. 确保 iPhone 与 OpenWrt 在同一 Wi-Fi 网络
2. 打开**控制中心**（从右上角下滑）
3. 长按**音乐播放区域**
4. 点击右上角的 **AirPlay 图标** (▶ 带三个圆环)
5. 选择你的设备名称（如 "我的音箱"）
6. 开始播放音乐，声音将从 OpenWrt 设备输出

### 在 Mac 上使用

1. 点击菜单栏的**声音图标**（或控制中心）
2. 选择输出设备为你的 AirPlay 设备
3. 或在音乐 App 中点击 AirPlay 图标选择设备

### 故障排除

```bash
# ---- Docker 部署 ----
# 检查容器内各服务状态
docker exec mediacenter ps aux | grep -E "shairport|avahi"

# 查看 avahi 发布的服务
docker exec mediacenter avahi-browse -a -t

# 查看容器日志
docker logs mediacenter | grep -i airplay

# 容器内测试音频
docker exec -it mediacenter speaker-test -t wav -c 2 -d 3

# ---- 非 Docker 部署 ----
# 检查 shairport-sync 是否运行
ps | grep shairport

# 检查 avahi 是否运行
ps | grep avahi

# 查看 shairport-sync 日志
logread | grep shairport

# ---- 通用 ----
# 检查端口是否监听
netstat -tlnp | grep 5000

# 测试音频输出
speaker-test -t wav -c 2
```

### 常见问题

**Q: iPhone 找不到 AirPlay 设备？**
- 确认在同一网络（同一子网）
- Docker 部署：确认使用的是 **macvlan** 或 **host** 网络，bridge 模式不支持 mDNS
- 检查 avahi-daemon 是否运行（Docker: `docker exec mediacenter ps aux | grep avahi`）
- 检查防火墙是否放行了 5000 端口和 mDNS（5353/UDP）

**Q: AirPlay 连接后没有声音？**
- 检查 `aplay -l` 确认声卡存在
- 检查 `amixer` 确认音量不为 0
- 尝试 `speaker-test -t wav` 测试

**Q: AirPlay 播放时背景音乐没有暂停？**
- 确认 mediacenter 已启动且 airplay 配置已启用
- 检查 `/tmp/shairport-sync-metadata` 管道是否存在

---

## DLNA 配置

DLNA 允许 Android 手机、Windows PC 等设备推送音频。

Docker 部署时 gmediarender 已内置在镜像中，无需额外安装。

非 Docker 部署：

```bash
# 安装
opkg install gmrender-resurrect

# 启动
/etc/init.d/gmrender enable
/etc/init.d/gmrender start
```

在 Android 上使用 BubbleUPnP、HiFi Cast 等 DLNA 客户端连接。

---

## 项目结构

```
openwrt-mediacenter/
├── Dockerfile                # Docker 构建文件
├── .dockerignore
├── config.yaml.example       # 配置模板
├── requirements.txt          # Python 依赖
├── mediacenter/
│   ├── __init__.py
│   ├── __main__.py           # python -m mediacenter 入口
│   ├── main.py               # 主服务，启动所有组件
│   ├── config.py             # 配置管理
│   ├── audio/
│   │   ├── manager.py        # 优先级音频管理器
│   │   ├── player.py         # mpv 播放器封装
│   │   ├── sources.py        # 音乐源解析 (yt-dlp)
│   │   └── background.py     # 背景音乐播放器
│   ├── airplay/
│   │   └── receiver.py       # AirPlay 接收 (shairport-sync)
│   ├── dlna/
│   │   └── renderer.py       # DLNA 渲染 (gmrender-resurrect)
│   ├── tts/
│   │   └── engine.py         # TTS 引擎 (edge-tts/OpenAI/Piper)
│   ├── scheduler/
│   │   └── jobs.py           # 定时任务
│   ├── ai/
│   │   └── agent.py          # AI 代理 (OpenAI 兼容)
│   └── api/
│       └── routes.py         # REST API (FastAPI)
└── scripts/
    ├── install_openwrt.sh    # 安装脚本
    ├── setup_airplay.sh      # AirPlay 配置脚本
    ├── docker_run.sh         # Docker 构建/运行脚本
    └── init.d_mediacenter    # 系统服务脚本
```

## 系统要求

- OpenWrt 21.02+ (或任意 Linux)
- Python 3.10+
- mpv 媒体播放器
- USB 声卡或板载音频
- (可选) shairport-sync (AirPlay)
- (可选) gmrender-resurrect (DLNA)
- (可选) yt-dlp (YouTube 等平台支持)

## 许可证

MIT License
