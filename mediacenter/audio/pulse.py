"""PulseAudio 客户端环境变量工具。

TTS ducking 完全靠系统层 module-role-ducking 按 sink-input 的 ``media.role``
匹配（见 README「TTS ducking」一节）。应用托管的每个发声子进程都必须带上
自己的 role，否则它不会被压低。

给子进程打 role 有两种方式：
- 程序自身支持（mpd 的 ``media_role`` 配置项）；
- libpulse 通用的 ``PULSE_PROP`` 环境变量：任何 libpulse 客户端在建立连接时
  都会解析它并合并进 client proplist，服务端再把 client proplist 合并进该
  客户端创建的 sink-input，因此对不支持自定义 proplist 的程序（如 shairport-sync，
  其 ``audio_pa.c`` 只读 ``pa.server`` / ``pa.application_name`` / ``pa.sink``）
  同样生效。

这里统一封装第二种方式，供 mpv（audio/player.py）和 shairport-sync
（airplay/receiver.py）共用，避免两处各自拼字符串。
"""

MEDIA_ROLE_KEY = "media.role"


def pulse_prop_with_role(current: str | None, role: str) -> str:
    """返回带 ``media.role=<role>`` 的 ``PULSE_PROP`` 值。

    ``current`` 是父进程环境里已有的 ``PULSE_PROP``（可为空）；其中已有的
    ``media.role=`` 项会被替换，其余属性原样保留。
    """
    parts = [
        item
        for item in (current or "").split()
        if not item.startswith(f"{MEDIA_ROLE_KEY}=")
    ]
    parts.append(f"{MEDIA_ROLE_KEY}={role}")
    return " ".join(parts)


def env_with_media_role(env: dict[str, str], role: str) -> dict[str, str]:
    """复制 ``env`` 并写入带 ``media.role=<role>`` 的 ``PULSE_PROP``。"""
    result = dict(env)
    result["PULSE_PROP"] = pulse_prop_with_role(env.get("PULSE_PROP"), role)
    return result
