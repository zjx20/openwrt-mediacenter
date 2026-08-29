# OpenWrt 媒体中心 (MediaCenter)

基于 Python 的 OpenWrt 多功能音频中心，支持**多优先级音频管理**、**AirPlay**、**DLNA** 和 **TTS 语音播报**。

## 功能特性

| 功能 | 说明 | 状态 |
|------|------|------|
| 🎵 背景音乐 | 支持 YouTube、B站、网易云、直接 URL 等 | ✅ |
| 📱 AirPlay | iPhone/iPad/Mac 无线推送音频 | ✅ |
| 📺 DLNA | Android/PC DLNA 推送音频 | ✅ |
| 🔊 TTS | 文字转语音播报 (edge-tts) | ✅ |
| 🔀 打断规则 | AirPlay / DLNA 互相硬重启互斥；mpv 被动让位 | ✅ |

### 音频流打断规则

| 触发事件 | 对 AirPlay | 对 DLNA | 对 mpv 背景音乐 |
|---------|-----------|---------|----------------|
| AirPlay 开始播放 | — | 重启服务（换设备名，断开手机端） | 暂停 |
| AirPlay 停止     | — | — | 恢复（若曾被本次打断暂停） |
| DLNA 开始串流    | 重启服务（kill shairport-sync） | — | 暂停 |
| DLNA 停止        | — | — | 恢复（若曾被本次打断暂停） |
| TTS 开始 / 结束  | —（不暂停） | —（不暂停） | —（不暂停，只被 PA 自动压低音量） |
| mpv 开始 / 停止  | — | — | — |

要点：

- **AirPlay 与 DLNA 之间是硬重启互斥**，不再用软暂停。原因：软暂停时手机端发现"暂停"
  状态会立刻自动 resume，打断不彻底；DLNA 重启时会换上新的随机后缀设备名（例如
  `客厅-a3f9` → `客厅-7k2m`），手机被迫重新选择目标，避免自动重连。
- **mpv 背景音乐被动让位** AirPlay / DLNA：起流时暂停，结束后恢复。
- **mpv 自身的播放、暂停、停止操作不会触发 AirPlay / DLNA 服务的状态变化** ——
  调 `/api/music/play` 或 `/api/music/stop` 不会重启对端服务。
- **TTS 不再显式打断任何流。** TTS 走独立 mpv 通道，输出 PulseAudio 流时附带
  `media.role=tts` 属性。其他流（背景音乐 `role=background`、AirPlay `role=airplay`、
  DLNA `role=dlna`）由 PulseAudio 在系统层自动 ducking：检测到 `role=tts` 时把
  这三类角色的音量压低（默认 -20dB），TTS 播完后立即恢复原音量。整个过程不暂停
  任何播放，背景音乐和正在投屏的会话都会继续，只是被压低到背景音。

#### PulseAudio ducking 配置

容器入口脚本 [`scripts/entrypoint.sh`](scripts/entrypoint.sh) 在启动时会自动向
宿主机 PulseAudio 加载 `module-role-ducking`：

```bash
pactl load-module module-role-ducking \
    trigger_roles=tts \
    ducking_roles=background,airplay,dlna \
    volume=-20dB \
    global=true
```

- 若宿主机 PA 已经加载过该模块（例如在 `default.pa` 里预设），脚本会跳过；
  若加载失败（PA 不支持该模块、权限不足等）也只打印一行警告并继续。
- 默认压低 20dB；可通过环境变量 `TTS_DUCK_VOLUME` 覆盖，例如
  `-e TTS_DUCK_VOLUME=-30dB` 让 TTS 期间其他流更安静。
- 非 Docker / 直装方式部署时，需要自行在宿主机 PA 配置同样的模块（写到
  `~/.config/pulse/default.pa` 或 `/etc/pulse/default.pa`），否则 TTS 期间
  其他音频不会被自动压低。

## 快速开始

### 1. 安装依赖 (OpenWrt)

> **先看这里：原版 OpenWrt 官方软件源撑不起完整的直装方式，推荐使用下文的 Docker 部署。**
> 以 23.05 官方源为准，直装有两个硬伤：
> - 源里**没有 `mpv`**，背景音乐和 TTS 通道无法工作，需要自行编译或从第三方源获取；
> - 官方 `shairport-sync-*` 编译时**不带 PulseAudio 后端**（`--with-pa`），也没有 MPRIS D-Bus 接口。
>   应用在 `audio.backend: pulse` 下会以 `-o pa` 启动 shairport-sync，用官方包会直接失败。
>   直装要自行编译 shairport-sync，至少 `--with-pa --with-metadata`，建议再加
>   `--with-dbus-interface --with-mpris-interface`（否则播放状态只能靠 stderr 标记，客户端异常断开时最多卡到 `session_timeout`）。
>
> 下面的安装脚本和手动命令只覆盖官方源里有的部分，`mpv` 和带 pa 后端的 shairport-sync 需要你自己补齐。

```bash
# 一键安装（官方源部分）
sh scripts/install_openwrt.sh

# 或手动安装
opkg update
opkg install python3 python3-pip alsa-utils pulseaudio-daemon pulseaudio-tools \
             avahi-dbus-daemon shairport-sync-openssl mpd-full upmpdcli
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

镜像内置了 shairport-sync (AirPlay)、mpd + upmpdcli (DLNA)、avahi-daemon (mDNS) 等所有组件，无需在宿主机额外安装。

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

[`scripts/docker_run.sh`](scripts/docker_run.sh) 会把当前目录的 `config.yaml`（不存在时自动从 `config.yaml.example` 复制）以只读方式挂载到容器内 `/etc/mediacenter/config.yaml`；可通过环境变量 `CONFIG_PATH` 指定其他配置文件路径。

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
docker run -d \
  --name mediacenter \
  --restart unless-stopped \
  --network host \
  -e PULSE_SERVER=unix:/run/pulse/native \
  -v /run/pulse:/run/pulse \
  -v $(pwd)/config.yaml:/etc/mediacenter/config.yaml:ro \
  -e AIRPLAY_NAME="客厅音箱" \
  openwrt-mediacenter
```

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

> ALSA 模式只覆盖背景音乐、TTS 和 AirPlay。**DLNA 在 ALSA 模式下不可用**——mpd 的输出固定走
> PulseAudio（需要 `media_role=dlna` 参与 TTS ducking）；同样地，没有 PulseAudio 也就没有
> TTS 播报时自动压低其他音频的效果。

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

Web UI 中也提供了 TTS 投递卡片：打开 [`/ui/`](mediacenter/static/index.html) 后，可直接输入播报文本，并在“设备音箱”和“当前浏览器”之间切换播放目标。

- 选择“设备音箱”时，前端调用 [`/api/tts/speak`](mediacenter/api/routes.py:198)，声音从设备侧输出。
- 选择“当前浏览器”时，前端直接播放 [`/api/tts/stream`](mediacenter/api/routes.py:217) 返回的音频流，只会在当前页面本地播出。
- 浏览器本地播放依赖页面的音频播放权限；若浏览器拦截自动播放，按页面提示重试即可。

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

如果直接在 OpenWrt 上运行（不用 Docker），只需要装好 shairport-sync 和 avahi，
并让 avahi 常驻；`shairport-sync` 进程本身和 Docker 方式一样由 `mediacenter` 应用托管启动，
设备名取 `config.yaml` 里的 `airplay.name`（环境变量 `AIRPLAY_NAME` 优先级更高）：

```bash
# 1. 安装 shairport-sync 和 avahi
#    注意：官方 shairport-sync-openssl 不带 PulseAudio 后端，见上文「安装依赖」的说明，
#    audio.backend 为 pulse 时需要换成自行编译的版本
opkg update
opkg install shairport-sync-openssl avahi-dbus-daemon

# 2. 让 avahi (mDNS 设备发现) 开机常驻（init.d 脚本名就叫 avahi-daemon）
/etc/init.d/avahi-daemon enable
/etc/init.d/avahi-daemon start
```

> **不要**把 shairport-sync 注册成 init.d 服务（`/etc/init.d/shairport-sync enable`）。
> 应用启动时会自己拉起一个 shairport-sync 进程，系统服务再起一个会互相抢占
> AirPlay 端口和设备名。如果之前启用过，先 `disable` 再 `stop`。

### 手动配置 shairport-sync

默认不需要任何配置文件：应用会根据 `config.yaml` 的音频后端自动生成一份运行时配置（含 PulseAudio `media_role = "airplay"`，TTS ducking 依赖它）。只有需要覆盖默认参数时才提供自己的配置：Docker 挂载到 `/etc/mediacenter/shairport-sync.conf`，直装则把文件路径填到 `config.yaml` 的 `airplay.config_path`。自定义配置示例：

```
general = {
    name = "我的音箱";          // AirPlay 设备名称
    output_backend = "pa";      // 使用 PulseAudio 输出（自动路由到蓝牙音箱）
};

// 使用自定义配置后应用不再自动注入 media_role，务必保留这一段，
// 否则 TTS 播报时 AirPlay 不会被 PulseAudio 自动压低音量
pa = {
    media_role = "airplay";
    // sink = "";               // 留空 = 使用系统默认 sink（蓝牙音箱等）
};

metadata = {
    enabled = "yes";
    pipe_name = "/tmp/shairport-sync-metadata";
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

# 查看 AirPlay 相关日志（shairport-sync 的输出由 mediacenter 捕获并写入应用日志）
logread | grep -i airplay

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
- 播放状态来自 shairport-sync 的 MPRIS D-Bus 信号：确认 dbus 正常运行，
  并在日志里找 `AirPlay` 状态变化记录；`curl /api/airplay/status` 的 `is_playing` 应随播放切换

---

## DLNA 配置

DLNA 允许 Android 手机、Windows PC 等设备推送音频。

实现方式是 **upmpdcli + mpd** 一对进程：upmpdcli 负责 UPnP MediaRenderer / OpenHome
协议层，把控制端的指令翻译成 MPD 命令；mpd 负责拉流、解码并通过 PulseAudio 输出
（带 `media_role=dlna`，供 TTS ducking 使用）。两者的配置文件由 `mediacenter` 在启动时
自动生成，进程也由应用托管（[`mediacenter/dlna/renderer.py`](mediacenter/dlna/renderer.py)），
`config.yaml` 里只需关心 `dlna.name`、`dlna.port`、`dlna.default_volume`。

Docker 部署时 mpd 和 upmpdcli 已内置在镜像中，无需额外安装。

非 Docker 部署只需装包，不要注册它们的系统服务：

```bash
# mpd 需要 pulse 输出插件和 curl 输入插件，mpd-mini 缺这些，必须用 mpd-full
opkg install mpd-full upmpdcli

# OpenWrt 装包时会自动 enable 这两个包自带的 init.d 服务，必须关掉，
# 否则会和 mediacenter 自己拉起的实例抢 6600 / UPnP 端口
/etc/init.d/mpd disable;      /etc/init.d/mpd stop
/etc/init.d/upmpdcli disable; /etc/init.d/upmpdcli stop
```

DLNA 依赖宿主机 PulseAudio（见上文「容器中使用音频设备」），`audio.backend: alsa` 时不可用。

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
│   │   ├── renderer.py       # DLNA 渲染 (mpd + upmpdcli 进程托管)
│   │   └── proxy.py          # mpd 拉流用的 HTTP 转发代理（上游断连自动 Range 续传）
│   ├── tts/
│   │   └── engine.py         # TTS 引擎 (edge-tts)
│   └── api/
│       └── routes.py         # REST API (FastAPI)
└── scripts/
    ├── install_openwrt.sh    # 安装脚本
    ├── docker_run.sh         # Docker 构建/运行脚本
    └── init.d_mediacenter    # 系统服务脚本
```

## 系统要求

- OpenWrt 21.02+ (或任意 Linux)
- Python 3.10+
- mpv 媒体播放器
- PulseAudio（所有音频通道默认经它输出；DLNA 和 TTS ducking 硬依赖）
- USB 声卡 / 板载音频 / 蓝牙音箱（由 PulseAudio 路由）
- (可选) shairport-sync（需带 PulseAudio 后端）+ avahi-daemon (AirPlay)
- (可选) mpd-full + upmpdcli (DLNA)
- (可选) yt-dlp (YouTube 等平台支持)

## 许可证

MIT License
