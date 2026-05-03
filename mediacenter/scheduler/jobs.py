"""定时任务模块"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

from croniter import croniter

logger = logging.getLogger(__name__)

USER_JOBS_FILE = Path("/etc/mediacenter/user_jobs.json")
SCRIPTS_DIR = Path("/etc/mediacenter/scripts")
LOGS_DIR = Path("/etc/mediacenter/job_logs")
LOG_MAX_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 300


def _seconds_until_next(cron_expr: str) -> float:
    base = datetime.now()
    nxt = croniter(cron_expr, base).get_next(datetime)
    return max(0.0, (nxt - base).total_seconds())


class Scheduler:
    """定时任务调度器"""

    def __init__(self, config: dict, tts_engine=None, background_player=None):
        self.config = config
        self.tts = tts_engine
        self.bg_player = background_player
        self._builtin_tasks: list[asyncio.Task] = []
        self._user_tasks: dict[str, asyncio.Task] = {}
        self._user_jobs: dict[str, dict] = {}
        self._running = False
        self._lock = asyncio.Lock()

    async def start(self):
        """启动调度器：注册内置任务 + 加载用户任务"""
        self._running = True

        # 内置任务（来自 config.yaml）
        for job in self.config.get("jobs", []):
            if not job.get("enabled", True):
                continue
            job_type = job.get("type")
            cron = job.get("cron", "")
            if job_type == "download_news":
                task = asyncio.create_task(
                    self._cron_loop(cron, self._download_news, job.get("id", "news"))
                )
                self._builtin_tasks.append(task)
                logger.info(f"内置定时任务已注册: {job.get('id')} ({cron})")

        # 用户任务（来自 user_jobs.json）
        self._user_jobs = {meta["id"]: meta for meta in self._read_user_jobs_file()}
        for meta in list(self._user_jobs.values()):
            await self._schedule_user_job(meta)
            logger.info(f"用户定时任务已注册: {meta['id']} ({meta.get('cron')})")

    async def _cron_loop(self, cron_expr: str, func, job_id: str):
        """基于 croniter 的调度循环：休眠到下一次触发时间，再执行"""
        try:
            croniter(cron_expr)
        except Exception as e:
            logger.error(f"cron 表达式无效，任务 {job_id} 不会运行: {cron_expr} ({e})")
            return

        while self._running:
            try:
                delay = _seconds_until_next(cron_expr)
            except Exception as e:
                logger.error(f"任务 {job_id} 计算下次触发时间失败: {e}")
                return
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            if not self._running:
                break
            logger.info(f"执行定时任务: {job_id}")
            try:
                await func()
            except Exception as e:
                logger.error(f"定时任务 {job_id} 执行失败: {e}")

    async def _download_news(self):
        """下载新闻联播

        从央视网抓取当天的新闻联播视频，提取音频保存到本地。
        """
        today = datetime.now().strftime("%Y%m%d")
        output_dir = Path("/tmp/news")
        output_dir.mkdir(exist_ok=True)
        output_file = output_dir / f"xinwen_{today}.mp3"

        if output_file.exists():
            logger.info(f"新闻联播已存在: {output_file}")
            return

        cctv_url = "https://tv.cctv.com/lm/xwlb/"
        logger.info(f"开始下载新闻联播: {today}")

        try:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp",
                "--extract-audio",
                "--audio-format", "mp3",
                "-o", str(output_file),
                cctv_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

            if proc.returncode == 0:
                logger.info(f"新闻联播下载完成: {output_file}")
                if self.tts:
                    await self.tts.speak("新闻联播已下载完成")
            else:
                logger.warning(f"新闻联播下载失败: {stderr.decode()[:200]}")
        except (FileNotFoundError, asyncio.TimeoutError) as e:
            logger.error(f"下载新闻联播出错: {e}")

    async def run_once(self, job_type: str):
        """手动触发一次任务（按内置类型）"""
        if job_type == "download_news":
            await self._download_news()
        else:
            raise ValueError(f"未知任务类型: {job_type}")

    async def stop(self):
        """停止调度器"""
        self._running = False
        all_tasks = list(self._builtin_tasks) + list(self._user_tasks.values())
        for task in all_tasks:
            task.cancel()
        for task in all_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._builtin_tasks.clear()
        self._user_tasks.clear()
        logger.info("调度器已停止")

    # ========== 用户任务存储 ==========

    def _read_user_jobs_file(self) -> list[dict]:
        if not USER_JOBS_FILE.exists():
            return []
        try:
            data = json.loads(USER_JOBS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"读取用户任务配置失败: {e}")
            return []

    def _write_user_jobs_file(self):
        USER_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USER_JOBS_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(list(self._user_jobs.values()), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(USER_JOBS_FILE)

    def _script_path(self, job_id: str, runtime: str) -> Path:
        ext = ".py" if runtime == "python" else ".sh"
        return SCRIPTS_DIR / f"{job_id}{ext}"

    def _log_path(self, job_id: str) -> Path:
        return LOGS_DIR / f"{job_id}.log"

    # ========== 用户任务调度 ==========

    async def _schedule_user_job(self, meta: dict):
        """为用户任务创建/重建调度协程"""
        old = self._user_tasks.pop(meta["id"], None)
        if old:
            old.cancel()
            try:
                await old
            except asyncio.CancelledError:
                pass
        if not meta.get("enabled", True):
            return
        if not self._running:
            return
        cron = meta["cron"]
        job_id = meta["id"]

        async def _run():
            await self._run_user_script(job_id)

        task = asyncio.create_task(self._cron_loop(cron, _run, job_id))
        self._user_tasks[job_id] = task

    async def _run_user_script(self, job_id: str):
        """执行一个用户脚本，捕获输出，记录状态"""
        meta = self._user_jobs.get(job_id)
        if not meta:
            logger.error(f"用户任务不存在: {job_id}")
            return

        runtime = meta.get("runtime", "shell")
        script = self._script_path(job_id, runtime)
        if not script.exists():
            logger.error(f"脚本文件不存在: {script}")
            meta["last_run"] = datetime.now().isoformat(timespec="seconds")
            meta["last_status"] = "missing_script"
            async with self._lock:
                self._write_user_jobs_file()
            return

        if runtime == "python":
            cmd = ["python3", str(script)]
        else:
            cmd = ["/bin/sh", str(script)]

        timeout = int(meta.get("timeout", DEFAULT_TIMEOUT))
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = self._log_path(job_id)

        status = "ok"
        with open(log_file, "wb") as f:
            header = (
                f"=== {datetime.now().isoformat(timespec='seconds')} "
                f"run {job_id} ({runtime}) ===\n"
            ).encode()
            f.write(header)
            f.flush()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=f,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                    if proc.returncode != 0:
                        status = f"exit_{proc.returncode}"
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    status = "timeout"
            except FileNotFoundError as e:
                f.write(f"interpreter not found: {e}\n".encode())
                status = "interpreter_missing"
            except Exception as e:
                f.write(f"execution error: {e}\n".encode())
                status = "error"

        meta["last_run"] = datetime.now().isoformat(timespec="seconds")
        meta["last_status"] = status
        async with self._lock:
            self._write_user_jobs_file()

    # ========== 用户任务 CRUD ==========

    def list_all_jobs(self) -> list[dict]:
        """列出所有任务（内置 + 用户）"""
        builtin = []
        for j in self.config.get("jobs", []):
            builtin.append({
                "id": j.get("id"),
                "name": j.get("id"),
                "cron": j.get("cron", ""),
                "type": j.get("type"),
                "enabled": j.get("enabled", True),
                "source": "builtin",
            })
        user = []
        for meta in self._user_jobs.values():
            user.append({
                "id": meta["id"],
                "name": meta.get("name", meta["id"]),
                "cron": meta.get("cron", ""),
                "runtime": meta.get("runtime", "shell"),
                "enabled": meta.get("enabled", True),
                "timeout": meta.get("timeout", DEFAULT_TIMEOUT),
                "last_run": meta.get("last_run"),
                "last_status": meta.get("last_status"),
                "source": "user",
            })
        return builtin + user

    def has_user_job(self, job_id: str) -> bool:
        return job_id in self._user_jobs

    def get_user_job(self, job_id: str) -> dict | None:
        meta = self._user_jobs.get(job_id)
        if not meta:
            return None
        result = dict(meta)
        result["source"] = "user"
        try:
            result["script"] = self._script_path(job_id, meta["runtime"]).read_text(
                encoding="utf-8"
            )
        except FileNotFoundError:
            result["script"] = ""
        return result

    async def add_user_job(self, meta: dict, script_content: str):
        """新建用户任务

        meta 字段: id, name, cron, runtime (shell|python), enabled, timeout
        """
        self._validate_meta(meta)
        async with self._lock:
            if meta["id"] in self._user_jobs:
                raise ValueError(f"任务 ID 已存在: {meta['id']}")
            SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
            self._script_path(meta["id"], meta["runtime"]).write_text(
                script_content, encoding="utf-8"
            )
            self._user_jobs[meta["id"]] = meta
            self._write_user_jobs_file()
            await self._schedule_user_job(meta)

    async def update_user_job(self, job_id: str, meta: dict, script_content: str):
        self._validate_meta(meta)
        async with self._lock:
            if job_id not in self._user_jobs:
                raise KeyError(job_id)
            old = self._user_jobs[job_id]
            # 不允许修改 id
            meta["id"] = job_id
            # 切换 runtime 时清掉旧脚本文件，避免遗留
            if old.get("runtime") != meta["runtime"]:
                self._script_path(job_id, old.get("runtime", "shell")).unlink(
                    missing_ok=True
                )
            SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
            self._script_path(job_id, meta["runtime"]).write_text(
                script_content, encoding="utf-8"
            )
            # 保留运行历史
            meta.setdefault("last_run", old.get("last_run"))
            meta.setdefault("last_status", old.get("last_status"))
            self._user_jobs[job_id] = meta
            self._write_user_jobs_file()
            await self._schedule_user_job(meta)

    async def delete_user_job(self, job_id: str):
        async with self._lock:
            meta = self._user_jobs.pop(job_id, None)
            if not meta:
                raise KeyError(job_id)
            task = self._user_tasks.pop(job_id, None)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._script_path(job_id, meta.get("runtime", "shell")).unlink(
                missing_ok=True
            )
            self._log_path(job_id).unlink(missing_ok=True)
            self._write_user_jobs_file()

    async def run_user_job_once(self, job_id: str):
        if job_id not in self._user_jobs:
            raise KeyError(job_id)
        await self._run_user_script(job_id)

    def read_job_log(self, job_id: str) -> str:
        path = self._log_path(job_id)
        if not path.exists():
            return ""
        try:
            data = path.read_bytes()
        except Exception as e:
            return f"(读取日志失败: {e})"
        if len(data) > LOG_MAX_BYTES:
            data = b"... (truncated, showing last 64KB) ...\n" + data[-LOG_MAX_BYTES:]
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def cron_preview(expr: str, n: int = 3) -> list[str]:
        base = datetime.now()
        it = croniter(expr, base)
        return [it.get_next(datetime).isoformat(timespec="seconds") for _ in range(n)]

    @staticmethod
    def _validate_meta(meta: dict):
        for field in ("id", "cron", "runtime"):
            if not meta.get(field):
                raise ValueError(f"缺少字段: {field}")
        if meta["runtime"] not in ("shell", "python"):
            raise ValueError("runtime 必须是 'shell' 或 'python'")
        if not meta["id"].replace("_", "").replace("-", "").isalnum():
            raise ValueError("id 只能包含字母、数字、下划线、连字符")
        try:
            croniter(meta["cron"])
        except Exception as e:
            raise ValueError(f"cron 表达式无效: {e}")
