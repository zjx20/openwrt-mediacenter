import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from mediacenter.api import routes
from mediacenter.audio.background import BackgroundPlayer
from mediacenter.audio.manager import AudioManager, AudioPriority
from mediacenter.audio.sources import Track
from mediacenter.main import MediaCenter
from mediacenter.stop_timer import StopTimer


class StopTimerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stopped = asyncio.Event()
        self.stop = AsyncMock(side_effect=self.stopped.set)
        self.timer = StopTimer(self.stop)

    async def asyncTearDown(self):
        await self.timer.shutdown()

    async def test_duration_fires_once_and_clears_pending_state(self):
        status = self.timer.schedule(duration=0.02)
        self.assertTrue(status["active"])
        self.assertGreater(status["remaining"], 0)
        self.stop.assert_not_awaited()
        await asyncio.wait_for(self.stopped.wait(), 1)
        await self.timer.shutdown()
        self.stop.assert_awaited_once()
        self.assertFalse(self.timer.get_status()["active"])
        self.assertIsNone(self.timer.get_status()["timestamp"])

    async def test_absolute_timestamp(self):
        timestamp = time.time() + 0.02
        self.assertEqual(self.timer.schedule(timestamp=timestamp)["timestamp"], timestamp)
        await asyncio.wait_for(self.stopped.wait(), 1)
        self.stop.assert_awaited_once()

    async def test_replacement_prevents_old_deadline_from_firing(self):
        self.timer.schedule(duration=0.02)
        self.timer.schedule(duration=60)
        await asyncio.sleep(0.05)
        self.stop.assert_not_awaited()
        self.assertTrue(self.timer.get_status()["active"])

    async def test_cancel_is_idempotent_and_prevents_stop(self):
        self.timer.schedule(duration=0.02)
        self.timer.cancel()
        self.timer.cancel()
        await asyncio.sleep(0.05)
        self.stop.assert_not_awaited()

    async def test_invalid_requests_preserve_timer(self):
        timestamp = self.timer.schedule(duration=60)["timestamp"]
        for params in ({}, {"duration": 1, "timestamp": time.time() + 1},
                       {"duration": 0}, {"duration": -1}, {"duration": True},
                       {"duration": float("nan")}, {"timestamp": float("inf")},
                       {"timestamp": time.time() - 1}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.timer.schedule(**params)
            self.assertEqual(self.timer.get_status()["timestamp"], timestamp)

    async def test_shutdown_cancels_pending_timer(self):
        self.timer.schedule(duration=0.02)
        await self.timer.shutdown()
        await asyncio.sleep(0.05)
        self.stop.assert_not_awaited()

    async def test_cancel_does_not_interrupt_stop_already_in_progress(self):
        release = asyncio.Event()

        async def stop():
            self.stopped.set()
            await release.wait()

        self.timer._stop = stop
        self.timer.schedule(duration=0.001)
        await asyncio.wait_for(self.stopped.wait(), 1)
        self.assertTrue(self.timer.cancel()["stopping"])
        shutdown = asyncio.create_task(self.timer.shutdown())
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        release.set()
        await asyncio.wait_for(shutdown, 1)
        self.assertFalse(self.timer.get_status()["stopping"])

    async def test_failure_is_reported_and_next_schedule_clears_error(self):
        self.timer._stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        self.timer.schedule(duration=0.001)
        with self.assertLogs("mediacenter.stop_timer", level="ERROR"):
            await asyncio.sleep(0.02)
        self.assertEqual(self.timer.get_status()["last_error"], "stop failed")
        self.assertFalse(self.timer.get_status()["active"])
        self.assertIsNone(self.timer.schedule(duration=60)["last_error"])


class StopTimerAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mc = MediaCenter()
        self.original_mc = routes._mc
        routes.init_api(self.mc)
        # ASGITransport 不触发应用启动，不依赖 mpv / 投屏服务。
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=routes.app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.mc.stop_timer.shutdown()
        await self.client.aclose()
        routes.init_api(self.original_mc)

    async def test_create_query_replace_delete_and_full_status(self):
        response = await self.client.post("/api/stop-timer", json={"duration": 60})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["active"])
        timestamp = time.time() + 120
        response = await self.client.post("/api/stop-timer", json={"timestamp": timestamp})
        self.assertEqual(response.json()["timestamp"], timestamp)
        response = await self.client.get("/api/stop-timer")
        self.assertEqual(response.json()["timestamp"], timestamp)
        response = await self.client.get("/api/status")
        self.assertEqual(response.json()["stop_timer"]["timestamp"], timestamp)
        response = await self.client.delete("/api/stop-timer")
        self.assertFalse(response.json()["active"])

    async def test_invalid_payloads_preserve_existing_timer(self):
        original = self.mc.stop_timer.schedule(duration=60)["timestamp"]
        for payload in ({}, {"duration": 1, "timestamp": time.time() + 60},
                        {"duration": 0}, {"duration": -1}, {"duration": True},
                        {"duration": "60"}, {"duration": "NaN"},
                        {"timestamp": None}, {"duration": []}):
            with self.subTest(payload=payload):
                response = await self.client.post("/api/stop-timer", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.mc.stop_timer.get_status()["timestamp"], original)
        response = await self.client.post(
            "/api/stop-timer", json={"timestamp": time.time() - 1}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.mc.stop_timer.get_status()["timestamp"], original)

    async def test_nonfinite_numbers_return_validation_error(self):
        original = self.mc.stop_timer.schedule(duration=60)["timestamp"]
        for value in ("1e999", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                response = await self.client.post(
                    "/api/stop-timer", content='{"duration": ' + value + '}',
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.mc.stop_timer.get_status()["timestamp"], original)


class StopPlaybackTests(unittest.IsolatedAsyncioTestCase):
    def make_mc(self):
        mc = MediaCenter()
        mc.audio_manager = AudioManager()
        mc.audio_manager.stop = AsyncMock()
        mc.background_player = SimpleNamespace(stop=AsyncMock())
        for name in ("airplay", "dlna"):
            setattr(mc, name, SimpleNamespace(
                stop=AsyncMock(), _restart_lock=asyncio.Lock(),
                _pending_peer_restart=None,
            ))
        return mc

    async def test_timer_stops_all_playback_and_prevents_background_resume(self):
        mc = self.make_mc()
        for channel in mc.audio_manager.channels.values():
            channel.was_playing = True
        mc.stop_timer.schedule(duration=0.001)
        await asyncio.sleep(0.02)
        await mc.stop_timer.shutdown()
        mc.background_player.stop.assert_awaited_once()
        mc.airplay.stop.assert_awaited_once()
        mc.dlna.stop.assert_awaited_once()
        self.assertCountEqual(
            mc.audio_manager.stop.await_args_list,
            [unittest.mock.call(AudioPriority.STREAM), unittest.mock.call(AudioPriority.TTS)],
        )
        self.assertFalse(any(ch.was_playing for ch in mc.audio_manager.channels.values()))

    async def test_failure_does_not_skip_other_services(self):
        mc = self.make_mc()
        mc.airplay.stop.side_effect = RuntimeError("unavailable")
        with self.assertRaisesRegex(RuntimeError, "airplay: unavailable"):
            await mc.stop_playback()
        mc.dlna.stop.assert_awaited_once()
        mc.background_player.stop.assert_awaited_once()
        mc.audio_manager.stop.assert_any_await(AudioPriority.TTS)

    async def test_pending_peer_restart_is_cancelled_before_stopping(self):
        mc = self.make_mc()
        pending = asyncio.create_task(asyncio.sleep(60))
        mc.airplay._pending_peer_restart = pending

        async def stop():
            self.assertTrue(pending.cancelled())

        mc.dlna.stop.side_effect = stop
        await mc.stop_playback()
        self.assertTrue(pending.cancelled())

    async def test_stop_during_url_resolution_does_not_restart_background(self):
        manager = AudioManager()
        manager.play = AsyncMock()
        manager.stop = AsyncMock()
        player = BackgroundPlayer(manager)
        started, release = asyncio.Event(), asyncio.Event()

        async def resolve(url):
            started.set()
            await release.wait()
            return "http://test/audio", {}

        with patch("mediacenter.audio.background.resolve_stream", side_effect=resolve):
            task = asyncio.create_task(player._play_track(Track(url="http://test", source="youtube")))
            await asyncio.wait_for(started.wait(), 1)
            await player.stop()
            release.set()
            await asyncio.wait_for(task, 1)
        manager.play.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
