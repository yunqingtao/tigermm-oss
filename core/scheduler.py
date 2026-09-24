"""
TigerScheduler — 定时任务引擎
支持 cron/interval/one-shot 三种模式。任务持久化到 data/cron_jobs.json。
"""
import json, time, logging, asyncio
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger("core.scheduler")


@dataclass
class CronJob:
    id: str
    name: str
    schedule: str
    prompt: str
    enabled: bool = True
    created_at: str = ""
    last_run: str = ""
    run_count: int = 0
    max_runs: int = 0
    status: str = "idle"


def _parse_schedule(schedule: str) -> dict:
    s = schedule.strip().lower()
    if "t" in s and "-" in s:
        try:
            ts = datetime.fromisoformat(s)
            delay = (ts - datetime.now()).total_seconds()
            return {"type": "oneshot", "delay": max(0, delay)}
        except ValueError:
            pass
    if s.startswith("every "):
        rest = s[6:]
        total_seconds = 0
        import re
        for m in re.finditer(r'(\d+)\s*(h|m|s)', rest):
            val = int(m.group(1))
            unit = m.group(2)
            if unit == 'h': total_seconds += val * 3600
            elif unit == 'm': total_seconds += val * 60
            elif unit == 's': total_seconds += val
        if total_seconds > 0:
            return {"type": "interval", "seconds": total_seconds}
    if len(s.split()) >= 5:
        return {"type": "cron", "cron_expr": s}
    try:
        return {"type": "interval", "seconds": int(s)}
    except ValueError:
        return {"type": "unknown"}


class TigerScheduler:
    def __init__(self, data_dir: Path, pipeline=None):
        self.data_dir = data_dir
        self.JOBS_FILE = data_dir / "cron_jobs.json"
        self.pipeline = pipeline
        self._jobs: dict = {}
        self._tasks: dict = {}
        self._running = False
        self._load()

    def _load(self):
        if self.JOBS_FILE.exists():
            try:
                data = json.loads(self.JOBS_FILE.read_text(encoding='utf-8'))
                for jd in data.get("jobs", []):
                    job = CronJob(**jd)
                    self._jobs[job.id] = job
                logger.info("Scheduler: %d jobs loaded", len(self._jobs))
            except Exception as e:
                logger.warning("Scheduler load: %s", e)

    def _save(self):
        data = {"jobs": [asdict(j) for j in self._jobs.values()]}
        self.JOBS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    def add(self, name: str, schedule: str, prompt: str, max_runs: int = 0) -> CronJob:
        import uuid
        job = CronJob(
            id=str(uuid.uuid4())[:8],
            name=name, schedule=schedule, prompt=prompt,
            created_at=datetime.now().isoformat()[:19], max_runs=max_runs,
        )
        self._jobs[job.id] = job
        self._save()
        logger.info("Scheduler: + '%s' (id=%s)", name, job.id)
        if self._running:
            self._schedule_one(job)
        return job

    def remove(self, job_id: str) -> bool:
        if job_id in self._jobs:
            self._cancel_task(job_id)
            del self._jobs[job_id]
            self._save()
            return True
        return False

    def list_jobs(self) -> list:
        return [{"id": j.id, "name": j.name, "schedule": j.schedule,
                 "enabled": j.enabled, "run_count": j.run_count, "last_run": j.last_run}
                for j in self._jobs.values()]

    def _cancel_task(self, job_id: str):
        if job_id in self._tasks:
            self._tasks[job_id].cancel()
            del self._tasks[job_id]

    def _schedule_one(self, job: CronJob):
        if not job.enabled:
            return
        if job.max_runs > 0 and job.run_count >= job.max_runs:
            job.status = "done"; self._save(); return
        parsed = _parse_schedule(job.schedule)

        async def _runner():
            try:
                delay = 0
                if parsed["type"] == "oneshot":
                    delay = parsed["delay"]
                elif parsed["type"] == "interval":
                    delay = parsed["seconds"]
                if delay > 0:
                    await asyncio.sleep(delay)
                job.status = "running"; self._save()
                logger.info("Scheduler: run '%s'", job.name)
                if self.pipeline:
                    result = await self.pipeline.process(job.prompt)
                job.run_count += 1
                job.last_run = datetime.now().isoformat()[:19]
                job.status = "idle"; self._save()
                if parsed["type"] == "interval" and (job.max_runs == 0 or job.run_count < job.max_runs):
                    self._schedule_one(job)
            except asyncio.CancelledError:
                job.status = "idle"; self._save()
            except Exception as e:
                job.status = "error"; self._save()
                logger.error("Scheduler: '%s' error: %s", job.name, e)

        self._tasks[job.id] = asyncio.create_task(_runner())

    def start(self):
        if self._running: return
        self._running = True
        for job in self._jobs.values():
            if job.enabled:
                self._schedule_one(job)

    def stop(self):
        self._running = False
        for jid in list(self._tasks):
            self._cancel_task(jid)


_scheduler = None

def get_scheduler(pipeline=None):
    global _scheduler
    if _scheduler is None:
        from config.settings import DATA_DIR
        _scheduler = TigerScheduler(DATA_DIR, pipeline=pipeline)
    elif pipeline and not _scheduler.pipeline:
        _scheduler.pipeline = pipeline
    return _scheduler
