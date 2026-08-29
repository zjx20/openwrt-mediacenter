"""REST API - FastAPI 路由"""

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(title="OpenWrt MediaCenter", version="0.1.0")

# 全局引用，在 main.py 中初始化
_mc = None


def init_api(media_center):
    global _mc
    _mc = media_center


def _get_mc():
    if _mc is None:
        raise HTTPException(500, "媒体中心未初始化")
    return _mc


# ========== 数据模型 ==========

class PlayRequest(BaseModel):
    url: str
    play_mode: Optional[str] = None


class PlaylistRequest(BaseModel):
    urls: list[str]
    play_mode: str = "repeat_all"


class TTSRequest(BaseModel):
    text: str
    volume: Optional[int] = None


class VolumeRequest(BaseModel):
    volume: int
    channel: str = "background"  # background / stream / tts


class SearchRequest(BaseModel):
    query: str
    source: str = "youtube"


class PlayModeRequest(BaseModel):
    mode: str  # sequential / shuffle / repeat_one / repeat_all


# ========== 系统状态 ==========

@app.get("/api/status")
async def get_status():
    """获取系统状态"""
    mc = _get_mc()
    return mc.get_full_status()


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ========== 背景音乐 ==========

@app.post("/api/music/play")
async def play_music(req: PlayRequest):
    """播放音乐 URL"""
    mc = _get_mc()
    if req.play_mode:
        mc.background_player.set_play_mode(req.play_mode)
    await mc.background_player.add_and_play(req.url)
    return {"status": "playing", "url": req.url}


@app.post("/api/music/playlist")
async def load_playlist(req: PlaylistRequest):
    """加载播放列表"""
    mc = _get_mc()
    await mc.background_player.load_playlist(req.urls, req.play_mode)
    await mc.background_player.start()
    return {"status": "playing", "tracks": len(mc.background_player.playlist.tracks)}


@app.post("/api/music/pause")
async def pause_music():
    mc = _get_mc()
    await mc.background_player.pause()
    return {"status": "paused"}


@app.post("/api/music/resume")
async def resume_music():
    mc = _get_mc()
    await mc.background_player.resume()
    return {"status": "resumed"}


@app.post("/api/music/stop")
async def stop_music():
    mc = _get_mc()
    await mc.background_player.stop()
    return {"status": "stopped"}


@app.post("/api/music/next")
async def next_track():
    mc = _get_mc()
    await mc.background_player.next()
    track = mc.background_player.playlist.current_track
    return {
        "status": "playing",
        "track": {"title": track.title, "artist": track.artist} if track else None,
    }


@app.post("/api/music/prev")
async def prev_track():
    mc = _get_mc()
    await mc.background_player.prev()
    track = mc.background_player.playlist.current_track
    return {
        "status": "playing",
        "track": {"title": track.title, "artist": track.artist} if track else None,
    }


@app.get("/api/music/status")
async def music_status():
    mc = _get_mc()
    return mc.background_player.get_status()


@app.get("/api/music/tracks")
async def music_tracks():
    """获取当前播放列表的所有曲目"""
    mc = _get_mc()
    pl = mc.background_player.playlist
    return {
        "current_index": pl.current_index,
        "play_mode": pl.play_mode.value,
        "tracks": [
            {
                "title": t.title or t.url,
                "artist": t.artist,
                "url": t.url,
                "source": t.source,
                "duration": t.duration,
            }
            for t in pl.tracks
        ],
    }


@app.post("/api/music/play_mode")
async def set_play_mode(req: PlayModeRequest):
    """切换播放模式"""
    mc = _get_mc()
    valid = {"sequential", "shuffle", "repeat_one", "repeat_all"}
    if req.mode not in valid:
        raise HTTPException(400, f"未知播放模式: {req.mode}")
    mc.background_player.set_play_mode(req.mode)
    return {"play_mode": req.mode}


@app.post("/api/music/search")
async def search_music(req: SearchRequest):
    """搜索音乐"""
    from mediacenter.audio.sources import search_music as do_search
    tracks = await do_search(req.query, req.source)
    return {
        "results": [
            {
                "url": t.url,
                "title": t.title,
                "artist": t.artist,
                "duration": t.duration,
            }
            for t in tracks
        ]
    }


# ========== TTS ==========

@app.post("/api/tts/speak")
async def tts_speak(req: TTSRequest):
    """TTS 语音播报"""
    from mediacenter.audio.manager import AudioPriority
    mc = _get_mc()
    if req.volume is not None:
        await mc.audio_manager.set_volume(AudioPriority.TTS, req.volume)
    await mc.tts.speak(req.text)
    return {"status": "done", "text": req.text}


@app.post("/api/tts/synthesize")
async def tts_synthesize(req: TTSRequest):
    """仅合成不播放，返回文件路径"""
    mc = _get_mc()
    path = await mc.tts.synthesize(req.text)
    return {"path": path}


@app.get("/api/tts/stream")
async def tts_stream(text: str):
    """TTS 流式播放（speak 路径专用）"""
    mc = _get_mc()
    return StreamingResponse(mc.tts.stream(text), media_type="audio/mpeg")


@app.post("/api/tts/clear-cache")
async def tts_clear_cache():
    mc = _get_mc()
    mc.tts.clear_cache()
    return {"status": "cleared"}


# ========== 音量控制 ==========

@app.post("/api/volume")
async def set_volume(req: VolumeRequest):
    """设置音量"""
    from mediacenter.audio.manager import AudioPriority

    mc = _get_mc()
    channel_map = {
        "background": AudioPriority.BACKGROUND,
        "stream": AudioPriority.STREAM,
        "tts": AudioPriority.TTS,
    }
    priority = channel_map.get(req.channel)
    if priority is None:
        raise HTTPException(400, f"未知通道: {req.channel}")
    await mc.audio_manager.set_volume(priority, req.volume)
    return {"volume": req.volume, "channel": req.channel}


# ========== AirPlay / DLNA 状态 ==========

@app.get("/api/airplay/status")
async def airplay_status():
    mc = _get_mc()
    return mc.airplay.get_status()


@app.get("/api/dlna/status")
async def dlna_status():
    mc = _get_mc()
    return mc.dlna.get_status()


@app.post("/api/stream/stop")
async def stream_stop():
    """暂停所有外部串流（AirPlay + DLNA），可从 Web UI 打断正在播放的串流"""
    mc = _get_mc()
    stopped = []
    if mc.airplay and mc.airplay.is_playing:
        await mc.airplay.pause()
        stopped.append("airplay")
    if mc.dlna and mc.dlna.is_streaming:
        await mc.dlna.pause()
        stopped.append("dlna")
    return {"stopped": stopped}


@app.post("/api/airplay/start")
async def airplay_start():
    """启动 shairport-sync（若已运行则跳过）。"""
    mc = _get_mc()
    if not mc.airplay:
        raise HTTPException(503, "AirPlay 未初始化")
    await mc.airplay.start()
    return {"status": "started", "service": "airplay"}


@app.post("/api/airplay/stop")
async def airplay_stop():
    """停止 shairport-sync，断开当前会话且不再自动重启。"""
    mc = _get_mc()
    if not mc.airplay:
        raise HTTPException(503, "AirPlay 未初始化")
    await mc.airplay.stop()
    return {"status": "stopped", "service": "airplay"}


@app.post("/api/airplay/restart")
async def airplay_restart():
    """强制停止并重启 shairport-sync，断开当前 AirPlay 会话。"""
    mc = _get_mc()
    if not mc.airplay:
        raise HTTPException(503, "AirPlay 未初始化")
    await mc.airplay.restart()
    return {"status": "restarted", "service": "airplay"}


@app.post("/api/dlna/start")
async def dlna_start():
    """启动 mpd + upmpdcli（若已运行则跳过）。"""
    mc = _get_mc()
    if not mc.dlna:
        raise HTTPException(503, "DLNA 未初始化")
    await mc.dlna.start()
    return {"status": "started", "service": "dlna"}


@app.post("/api/dlna/stop")
async def dlna_stop():
    """停止 mpd + upmpdcli，断开当前会话且不再自动重启。"""
    mc = _get_mc()
    if not mc.dlna:
        raise HTTPException(503, "DLNA 未初始化")
    await mc.dlna.stop()
    return {"status": "stopped", "service": "dlna"}


@app.post("/api/dlna/restart")
async def dlna_restart():
    """强制停止并重启 mpd + upmpdcli，断开当前 DLNA 会话。"""
    mc = _get_mc()
    if not mc.dlna:
        raise HTTPException(503, "DLNA 未初始化")
    await mc.dlna.restart()
    return {"status": "restarted", "service": "dlna"}


# ========== 静态 UI ==========

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if _STATIC_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(_STATIC_DIR), html=True), name="ui")
