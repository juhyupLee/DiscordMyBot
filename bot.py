import os
import json
import asyncio
import subprocess
import tempfile
import logging
import logging.handlers
import shlex
import re
import time
import glob
import signal
from datetime import datetime, timedelta
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
WORKING_DIR = os.getenv("WORKING_DIR", os.getcwd())
CLAUDE_CATEGORY_ID = int(os.getenv("CLAUDE_CATEGORY_ID", "0"))
CLAUDE_PATH = os.getenv("CLAUDE_PATH", os.path.expanduser(r"~\.local\bin\claude.exe"))
DEFAULT_BACKEND = os.getenv("DEFAULT_BACKEND", "codex").strip().lower()
if DEFAULT_BACKEND not in ("claude", "codex"):
    DEFAULT_BACKEND = "codex"

# 채널별 장기 실행 Claude 프로세스 세션
# {channel_id: {"proc", "work_dir", "reader_task", "event_queue", "last_active", "session_id", "lock"}}
channel_claude_procs: dict[int, dict] = {}
# 채널별 작업 실행 중 여부
channel_busy: dict[int, bool] = {}
# 채널별 실행 중인 _process_channel 태스크 (중지 시 cancel 용)
channel_tasks: dict[int, asyncio.Task] = {}
# 채널별 실행 중인 Codex 단발 프로세스와 상태 메시지 리소스
# {channel_id: {"proc", "status_task", "status_msg"}}
channel_codex_runs: dict[int, dict] = {}

# 채널별 대기 중인 메시지 큐는 아래 CHANNEL_PENDING_FILE에서 복원한다.
# 각 항목은 prompt/message_id/image_paths/created_at을 가진 dict이며, 실행 중에는
# Discord message 객체를 선택적으로 "message" 키에 보관한다.
channel_pending: dict[int, list[dict]] = {}
# 채널별 현재 작업 상태 텍스트
channel_status: dict[int, str] = {}
# 채널별 직전 턴에서 Claude가 Write/Edit한 파일 (절대경로) — /files 다운로드용
channel_recent_files: dict[int, list[str]] = {}

# 채널별 작업 디렉토리 & 세션 (파일에 저장하여 재시작해도 유지)
WORKDIRS_FILE = os.path.join(os.path.dirname(__file__), "channel_workdirs.json")
SESSIONS_FILE = os.path.join(os.path.dirname(__file__), "channel_sessions.json")
MODELS_FILE = os.path.join(os.path.dirname(__file__), "channel_models.json")
BACKENDS_FILE = os.path.join(os.path.dirname(__file__), "channel_backends.json")
CODEX_SESSIONS_FILE = os.path.join(os.path.dirname(__file__), "channel_codex_sessions.json")
CODEX_MODELS_FILE = os.path.join(os.path.dirname(__file__), "channel_codex_models.json")
CODEX_REASONING_FILE = os.path.join(os.path.dirname(__file__), "channel_reasoning.json")
CODEX_DEFERRED_FILE = os.path.join(os.path.dirname(__file__), "codex_deferred_jobs.json")
CODEX_USAGE_FILE = os.path.join(os.path.dirname(__file__), "codex_usage_stats.json")
CHANNEL_PENDING_FILE = os.path.join(os.path.dirname(__file__), "channel_pending_jobs.json")
CODEX_MODELS_CACHE_FILE = os.getenv(
    "CODEX_MODELS_CACHE_FILE",
    os.path.join(os.path.expanduser("~"), ".codex", "models_cache.json"),
)
SCHEDULES_FILE = os.path.join(os.path.dirname(__file__), "scheduled_jobs.json")
SCHEDULE_RUNTIME_FIELDS = {"running", "started_at"}
CODEX_DEFERRED_RUNTIME_FIELDS = {"running", "started_at"}
CODEX_LIMIT_RETRY_GRACE_SECONDS = 60
CODEX_DEFERRED_BUSY_RETRY_MINUTES = 5
CODEX_DEFERRED_FALLBACK_RETRY_MINUTES = 60
CODEX_CLAUDE_AUDIT_FAILURE_THRESHOLD = 3
CODEX_CLAUDE_AUDIT_INTERVAL_MINUTES = 60
CODEX_USAGE_RETENTION_DAYS = 90
CODEX_RATE_LIMIT_CACHE_SECONDS = 5 * 60
CODEX_RATE_LIMIT_FAILURE_CACHE_SECONDS = 60
JSONL_MAX_LINE_BYTES = 64 * 1024 * 1024
CODEX_USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


class JsonlLineTooLargeError(ValueError):
    pass


async def _discard_jsonl_line(reader: asyncio.StreamReader):
    """현재 JSONL 줄의 나머지를 버리고 다음 줄 경계로 복구한다."""
    while True:
        try:
            await reader.readuntil(b"\n")
            return
        except asyncio.LimitOverrunError as error:
            consume = max(1, int(error.consumed))
            try:
                await reader.readexactly(consume)
            except asyncio.IncompleteReadError:
                return
        except asyncio.IncompleteReadError:
            return


async def _read_jsonl_line(
    reader: asyncio.StreamReader,
    *,
    max_bytes: int = JSONL_MAX_LINE_BYTES,
) -> bytes:
    """StreamReader 제한보다 긴 JSONL 한 줄도 청크로 회수해 온전하게 반환한다."""
    chunks: list[bytes] = []
    total = 0

    while True:
        try:
            chunk = await reader.readuntil(b"\n")
        except asyncio.LimitOverrunError as error:
            consume = max(1, int(error.consumed))
            if total + consume > max_bytes:
                await _discard_jsonl_line(reader)
                raise JsonlLineTooLargeError(
                    f"JSONL line exceeds safety limit ({max_bytes} bytes)"
                )
            try:
                chunk = await reader.readexactly(consume)
            except asyncio.IncompleteReadError as incomplete:
                chunk = incomplete.partial
                total += len(chunk)
                if total > max_bytes:
                    raise JsonlLineTooLargeError(
                        f"JSONL line exceeds safety limit ({max_bytes} bytes)"
                    )
                chunks.append(chunk)
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            continue
        except asyncio.IncompleteReadError as incomplete:
            chunk = incomplete.partial
            if not chunk and not chunks:
                return b""
            total += len(chunk)
            if total > max_bytes:
                raise JsonlLineTooLargeError(
                    f"JSONL line exceeds safety limit ({max_bytes} bytes)"
                )
            chunks.append(chunk)
            return b"".join(chunks)

        total += len(chunk)
        if total > max_bytes:
            raise JsonlLineTooLargeError(
                f"JSONL line exceeds safety limit ({max_bytes} bytes)"
            )
        chunks.append(chunk)
        return b"".join(chunks)

def load_json_dict(path: str) -> dict[int, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {int(k): v for k, v in json.load(f).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_json_dict(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in data.items()}, f, ensure_ascii=False, indent=2)

def _persistable_schedule_job(job: dict) -> dict:
    return {k: v for k, v in job.items() if k not in SCHEDULE_RUNTIME_FIELDS}

def load_schedules() -> dict[str, dict]:
    try:
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {
            str(job_id): _persistable_schedule_job(job)
            for job_id, job in data.items()
            if isinstance(job, dict)
        }
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def refresh_schedules_from_disk():
    for job_id, job in load_schedules().items():
        scheduled_jobs.setdefault(job_id, job)

def save_schedules(*, deleted_ids: set[str] | None = None):
    disk_jobs = load_schedules()
    merged = {**disk_jobs, **scheduled_jobs}
    for job_id in deleted_ids or set():
        merged.pop(str(job_id), None)

    persisted = {
        str(job_id): _persistable_schedule_job(job)
        for job_id, job in merged.items()
        if isinstance(job, dict)
    }
    tmp_path = f"{SCHEDULES_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(persisted, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, SCHEDULES_FILE)

    scheduled_jobs.clear()
    scheduled_jobs.update(merged)

def load_codex_deferred_jobs() -> dict[int, dict]:
    try:
        with open(CODEX_DEFERRED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        jobs: dict[int, dict] = {}
        for channel_id, job in data.items():
            if not isinstance(job, dict):
                continue
            clean = {k: v for k, v in job.items() if k not in CODEX_DEFERRED_RUNTIME_FIELDS}
            jobs[int(channel_id)] = clean
        return jobs
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError) as e:
        if not isinstance(e, FileNotFoundError):
            logger.warning(f"Codex 자동 재개 대기열 로드 실패: {e}")
        return {}

def save_codex_deferred_jobs():
    payload = {
        str(channel_id): {
            k: v for k, v in job.items()
            if k not in CODEX_DEFERRED_RUNTIME_FIELDS
        }
        for channel_id, job in codex_deferred_jobs.items()
        if isinstance(job, dict)
    }
    tmp_path = f"{CODEX_DEFERRED_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CODEX_DEFERRED_FILE)


def load_channel_pending() -> dict[int, list[dict]]:
    """재시작 전에 대기 중이던 일반 메시지를 안전한 직렬화 형태로 복원한다."""
    try:
        with open(CHANNEL_PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        restored: dict[int, list[dict]] = {}
        for channel_id, entries in data.items():
            if not isinstance(entries, list):
                continue
            clean_entries = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                prompt = str(entry.get("prompt", "")).strip()
                if not prompt:
                    continue
                images = entry.get("image_paths")
                clean_entries.append({
                    "prompt": prompt,
                    "message_id": entry.get("message_id"),
                    "image_paths": [str(path) for path in images if path]
                    if isinstance(images, list) else [],
                    "created_at": str(entry.get("created_at") or ""),
                })
            if clean_entries:
                restored[int(channel_id)] = clean_entries
        return restored
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError) as e:
        if not isinstance(e, FileNotFoundError):
            logger.warning(f"채널 대기열 로드 실패: {e}")
        return {}


def save_channel_pending():
    payload = {}
    for channel_id, entries in channel_pending.items():
        clean_entries = []
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("prompt", "")).strip():
                continue
            clean_entries.append({
                "prompt": str(entry["prompt"]),
                "message_id": entry.get("message_id"),
                "image_paths": list(entry.get("image_paths") or []),
                "created_at": str(entry.get("created_at") or ""),
            })
        if clean_entries:
            payload[str(channel_id)] = clean_entries
    tmp_path = f"{CHANNEL_PENDING_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CHANNEL_PENDING_FILE)


def _queue_channel_pending(channel_id: int, prompt: str, message, image_paths: list[str]):
    entry = {
        "prompt": prompt,
        "message_id": getattr(message, "id", None),
        "message": message,
        "image_paths": list(image_paths or []),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    channel_pending.setdefault(channel_id, []).append(entry)
    save_channel_pending()
    return entry


def _pop_channel_pending(channel_id: int) -> list[dict]:
    entries = channel_pending.pop(channel_id, None) or []
    save_channel_pending()
    return entries


def _clear_channel_pending(channel_id: int) -> bool:
    existed = bool(channel_pending.pop(channel_id, None))
    save_channel_pending()
    return existed

def load_codex_usage_stats() -> dict:
    try:
        with open(CODEX_USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": 1, "days": {}}
        days = data.get("days")
        if not isinstance(days, dict):
            days = {}
        return {"version": 1, "days": days}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"version": 1, "days": {}}

def save_codex_usage_stats():
    tmp_path = f"{CODEX_USAGE_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(codex_usage_stats, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CODEX_USAGE_FILE)

def save_workdirs():
    save_json_dict(WORKDIRS_FILE, channel_workdirs)

def save_sessions():
    save_json_dict(SESSIONS_FILE, channel_sessions)

def save_models():
    save_json_dict(MODELS_FILE, channel_models)

def save_backends():
    save_json_dict(BACKENDS_FILE, channel_backends)

def save_codex_sessions():
    save_json_dict(CODEX_SESSIONS_FILE, channel_codex_sessions)

def save_codex_models():
    save_json_dict(CODEX_MODELS_FILE, channel_codex_models)

def save_codex_reasoning():
    save_json_dict(CODEX_REASONING_FILE, channel_reasoning)

channel_workdirs: dict[int, str] = load_json_dict(WORKDIRS_FILE)
channel_sessions: dict[int, str] = load_json_dict(SESSIONS_FILE)
channel_models: dict[int, str] = load_json_dict(MODELS_FILE)
channel_backends: dict[int, str] = load_json_dict(BACKENDS_FILE)  # "claude" | "codex"
channel_codex_sessions: dict[int, str] = load_json_dict(CODEX_SESSIONS_FILE)  # 채널별 codex thread_id
channel_codex_models: dict[int, str] = load_json_dict(CODEX_MODELS_FILE)  # 채널별 Codex model slug
channel_reasoning: dict[int, str] = load_json_dict(CODEX_REASONING_FILE)  # 채널별 Codex reasoning effort
scheduled_jobs: dict[str, dict] = load_schedules()
for _job in scheduled_jobs.values():
    _job.pop("running", None)
    _job.pop("started_at", None)
codex_deferred_jobs: dict[int, dict] = load_codex_deferred_jobs()
for _job in codex_deferred_jobs.values():
    _job.pop("running", None)
    _job.pop("started_at", None)
channel_pending.update(load_channel_pending())
codex_usage_stats: dict = load_codex_usage_stats()
codex_rate_limit_cache: dict = {
    "expires_at": 0.0,
    "state": None,
}
scheduler_task: asyncio.Task | None = None

MODEL_ALIASES = ["opus", "sonnet", "haiku"]
BACKEND_OPTIONS = ["claude", "codex"]
SELECT_MODEL_OPTIONS = [
    {
        "value": "codex",
        "label": "Codex default",
        "backend": "codex",
        "model": None,
        "description": "환경설정의 Codex 기본 모델",
    },
    {
        "value": "claude-opus-5",
        "label": "Claude Opus 5.0",
        "backend": "claude",
        "model": "claude-opus-5",
        "description": "Claude Opus 5.0 고정",
    },
    {
        "value": "claude-opus-4-8",
        "label": "Claude Opus 4.8",
        "backend": "claude",
        "model": "claude-opus-4-8",
        "description": "Claude Opus 4.8 고정",
    },
    {
        "value": "claude-opus-4-7",
        "label": "Claude Opus 4.7",
        "backend": "claude",
        "model": "claude-opus-4-7",
        "description": "Claude Opus 4.7 고정",
    },
    {
        "value": "claude-opus-4-6",
        "label": "Claude Opus 4.6",
        "backend": "claude",
        "model": "claude-opus-4-6",
        "description": "Claude Opus 4.6 고정",
    },
    {
        "value": "opus",
        "label": "Claude Opus alias",
        "backend": "claude",
        "model": "opus",
        "description": "제공자별 최신 Opus alias",
    },
    {
        "value": "sonnet",
        "label": "Claude Sonnet alias",
        "backend": "claude",
        "model": "sonnet",
        "description": "제공자별 최신 Sonnet alias",
    },
    {
        "value": "haiku",
        "label": "Claude Haiku alias",
        "backend": "claude",
        "model": "haiku",
        "description": "빠른 Haiku alias",
    },
]
SELECT_MODEL_ALIASES = {
    "default": "codex",
    "highest": "codex",
    "codex-highest": "codex",
    "gpt-5.6": "gpt-5.6-sol",
    "opus-5": "claude-opus-5",
    "opus-5.0": "claude-opus-5",
    "opus5": "claude-opus-5",
    "opus-4.8": "claude-opus-4-8",
    "opus-4-8": "claude-opus-4-8",
    "opus-4.7": "claude-opus-4-7",
    "opus-4-7": "claude-opus-4-7",
    "opus-4.6": "claude-opus-4-6",
    "opus-4-6": "claude-opus-4-6",
}
SCHEDULE_MODES = ["daily", "every", "once"]


def _parse_local_dt(value: str) -> datetime:
    value = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError("날짜 형식은 `YYYY-MM-DD HH:MM` 이어야 합니다.")


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        h, m = value.strip().split(":", 1)
        hour, minute = int(h), int(m)
    except Exception:
        raise ValueError("시간 형식은 `HH:MM` 이어야 합니다.")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("시간은 `00:00`부터 `23:59` 사이여야 합니다.")
    return hour, minute


def _parse_interval(value: str) -> timedelta:
    v = value.strip().lower().replace(" ", "")
    if len(v) < 2:
        raise ValueError("반복 간격은 `30m`, `2h`, `1d`처럼 입력하세요.")
    unit = v[-1]
    try:
        amount = int(v[:-1])
    except ValueError:
        raise ValueError("반복 간격 숫자를 읽을 수 없습니다. 예: `30m`, `2h`")
    if amount <= 0:
        raise ValueError("반복 간격은 1 이상이어야 합니다.")
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    raise ValueError("반복 간격 단위는 `m`, `h`, `d`만 지원합니다.")


def _next_run_for(mode: str, when: str, *, after: datetime | None = None) -> datetime | None:
    now = after or datetime.now()
    mode = mode.lower().strip()
    if mode == "daily":
        hour, minute = _parse_hhmm(when)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    if mode == "every":
        return now + _parse_interval(when)
    if mode == "once":
        target = _parse_local_dt(when)
        if target <= now:
            raise ValueError("once 예약 시간은 현재보다 미래여야 합니다.")
        return target
    raise ValueError("mode는 `daily`, `every`, `once` 중 하나여야 합니다.")


def _format_schedule(job_id: str, job: dict) -> str:
    channel_id = job.get("channel_id")
    prompt = str(job.get("prompt", ""))
    if len(prompt) > 80:
        prompt = prompt[:77] + "..."
    return (
        f"`{job_id}` `{job.get('mode')}` `{job.get('when')}` "
        f"next `{job.get('next_run')}` ch `<#{channel_id}>` - {prompt}"
    )


def _add_schedule_job(mode: str, when: str, prompt: str, channel_id: int) -> tuple[str, dict]:
    refresh_schedules_from_disk()
    mode = mode.lower().strip()
    if mode not in SCHEDULE_MODES:
        raise ValueError("mode는 `daily`, `every`, `once` 중 하나여야 합니다.")
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("prompt가 비어 있습니다.")
    next_run = _next_run_for(mode, when)
    numeric_ids = [int(k) for k in scheduled_jobs.keys() if str(k).isdigit()]
    job_id = str((max(numeric_ids) if numeric_ids else 0) + 1)
    job = {
        "id": job_id,
        "mode": mode,
        "when": when.strip(),
        "prompt": prompt,
        "channel_id": channel_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "next_run": next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None,
    }
    scheduled_jobs[job_id] = job
    save_schedules()
    logger.info(
        f"예약 추가: id={job_id}, mode={mode}, when={when}, "
        f"channel={channel_id}, next={job['next_run']}, prompt={prompt[:80]}"
    )
    return job_id, job


def _resolve_codex_path() -> str:
    """codex 실행 파일 경로 — .env > shutil.which > 'codex'"""
    p = os.getenv("CODEX_PATH")
    if p:
        return p
    import shutil as _sh
    return _sh.which("codex") or "codex"


def _read_codex_config() -> dict:
    """~/.codex/config.toml을 파싱해서 dict로 반환 (실패 시 빈 dict)."""
    config_path = os.path.expanduser(r"~\.codex\config.toml")
    try:
        import tomllib
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        logger.debug(f"Codex config 읽기 실패: {e}")
        return {}


def _toml_dotted_key_segment(value: str) -> str:
    """Codex -c dotted key에 안전하게 사용할 TOML key segment를 만든다."""
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unavailable_local_mcp_overrides(config: dict | None = None) -> list[str]:
    """꺼져 있는 localhost HTTP MCP만 이번 Codex 실행에서 비활성화한다."""
    import socket
    from urllib.parse import urlparse

    config = config if isinstance(config, dict) else _read_codex_config()
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return []

    overrides = []
    for name, server in servers.items():
        if not isinstance(server, dict) or server.get("enabled") is False:
            continue
        url = server.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if host not in ("127.0.0.1", "localhost", "::1"):
                continue
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=0.2):
                pass
        except (OSError, ValueError):
            segment = _toml_dotted_key_segment(str(name))
            overrides.append(f"mcp_servers.{segment}.enabled=false")
    return overrides


def _resolve_codex_model(channel_id: int | None = None) -> str | None:
    """Codex 모델 — 채널 선택 > .env > config.toml > gpt-5.6-sol.

    전역 config는 플러그인 로드를 위해 사용하되, 채널별 선택이 항상 우선하도록
    여기서 모델 값을 정해 -m 플래그로 전달한다.
    """
    if channel_id is not None:
        selected = channel_codex_models.get(channel_id)
        if isinstance(selected, str) and selected.strip():
            return selected.strip()
    p = os.getenv("CODEX_MODEL")
    if p:
        return p.strip()
    model = _read_codex_config().get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return "gpt-5.6-sol"


REASONING_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
REASONING_LABELS = {
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "XHigh",
    "max": "Max",
    "ultra": "Ultra",
}


def _read_codex_models_cache() -> dict:
    try:
        with open(CODEX_MODELS_CACHE_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.warning(f"Codex 모델 캐시 읽기 실패: {e}")
        return {}


def _codex_model_cache_entry(model: str | None) -> dict | None:
    if not model:
        return None
    for item in _read_codex_models_cache().get("models", []):
        if isinstance(item, dict) and str(item.get("slug", "")).casefold() == model.casefold():
            return item
    return None


def _codex_supported_reasoning(model: str | None) -> list[dict]:
    entry = _codex_model_cache_entry(model)
    levels = entry.get("supported_reasoning_levels", []) if entry else []
    supported = []
    seen = set()
    for level in levels:
        if not isinstance(level, dict):
            continue
        effort = str(level.get("effort", "")).strip().lower()
        if not effort or effort in seen:
            continue
        seen.add(effort)
        supported.append({
            "effort": effort,
            "description": str(level.get("description", "")).strip(),
        })
    return supported


def _normalize_reasoning_effort(value: str) -> str:
    key = value.strip().lower()
    aliases = {
        "extra-high": "xhigh",
        "extra_high": "xhigh",
        "maximum": "max",
        "최대": "max",
        "울트라": "ultra",
    }
    return aliases.get(key, key)


def _default_codex_reasoning(model: str | None = None) -> str:
    """환경/config 기본값을 모델 지원 범위에 맞춰 반환한다."""
    model = model or _resolve_codex_model()
    configured = os.getenv("CODEX_REASONING_EFFORT")
    if not configured:
        config_effort = _read_codex_config().get("model_reasoning_effort")
        configured = config_effort if isinstance(config_effort, str) else ""
    effort = _normalize_reasoning_effort(configured or "xhigh")
    supported = [level["effort"] for level in _codex_supported_reasoning(model)]
    if not supported or effort in supported:
        return effort if effort in REASONING_ORDER else "xhigh"
    for fallback in ("xhigh", "high", "medium", "low"):
        if fallback in supported:
            return fallback
    return supported[0]


def _resolve_codex_reasoning(channel_id: int | None = None, model: str | None = None) -> str:
    """Codex reasoning effort — 채널 선택 > .env > config.toml > xhigh."""
    model = model or _resolve_codex_model(channel_id)
    supported = [level["effort"] for level in _codex_supported_reasoning(model)]
    if channel_id is not None:
        selected = channel_reasoning.get(channel_id)
        if isinstance(selected, str) and selected.strip():
            effort = _normalize_reasoning_effort(selected)
            if not supported or effort in supported:
                return effort
    return _default_codex_reasoning(model)


def _reasoning_options(channel_id: int) -> list[dict]:
    model = _resolve_codex_model(channel_id)
    default = _default_codex_reasoning(model)
    options = [{
        "value": "default",
        "label": f"Default ({REASONING_LABELS.get(default, default)})",
        "description": "환경/config 기본값 사용",
    }]
    for level in _codex_supported_reasoning(model):
        effort = level["effort"]
        options.append({
            "value": effort,
            "label": REASONING_LABELS.get(effort, effort.title()),
            "description": level["description"] or "Codex 추론 강도",
        })
    return options


def _reasoning_choices(channel_id: int, current: str = "") -> list[app_commands.Choice]:
    query = current.strip().casefold()
    choices = []
    for option in _reasoning_options(channel_id):
        haystack = f"{option['value']} {option['label']} {option['description']}".casefold()
        if query and query not in haystack:
            continue
        label = f"{option['label']} - {option['description']}"
        choices.append(app_commands.Choice(name=label[:100], value=option["value"]))
    return choices[:25]


def _reasoning_display(channel_id: int) -> str:
    effort = _resolve_codex_reasoning(channel_id)
    source = "channel" if channel_id in channel_reasoning else "default"
    return f"{effort} ({source})"


def apply_selected_reasoning(channel_id: int, value: str) -> tuple[bool, str]:
    if channel_backends.get(channel_id, DEFAULT_BACKEND) != "codex":
        return False, "추론 강도 선택은 Codex 백엔드에서만 사용할 수 있습니다."

    key = _normalize_reasoning_effort(value)
    if key in ("default", "clear", "기본", "해제"):
        channel_reasoning.pop(channel_id, None)
        save_codex_reasoning()
        return True, f"🧠 추론 강도: `{_resolve_codex_reasoning(channel_id)}` (기본값, 다음 메시지부터 적용)"

    model = _resolve_codex_model(channel_id)
    supported = [level["effort"] for level in _codex_supported_reasoning(model)]
    if key not in REASONING_ORDER or (supported and key not in supported):
        available = " / ".join(supported or REASONING_ORDER)
        return False, f"`{model}`에서 지원하지 않는 추론 강도: `{value}` (사용 가능: {available})"

    channel_reasoning[channel_id] = key
    save_codex_reasoning()
    return True, f"🧠 추론 강도: `{key}` / 모델: `{model}` (다음 메시지부터 적용)"


def _codex_exec_base_args(channel_id: int) -> list[str]:
    """전역 플러그인과 채널별 모델/추론 설정을 반영한 Codex exec 인자."""
    args = [
        "--dangerously-bypass-approvals-and-sandbox",
        "--skip-git-repo-check",
        "--json",
    ]
    for override in _unavailable_local_mcp_overrides():
        args += ["-c", override]
    model = _resolve_codex_model(channel_id)
    if model:
        args += ["-m", model]
    args += ["-c", f'model_reasoning_effort="{_resolve_codex_reasoning(channel_id, model)}"']
    return args


def _codex_command(args: list[str]) -> list[str]:
    """Windows npm shim을 포함해 Codex 관리 명령을 실행 가능한 argv로 만든다."""
    if os.name == "nt" and CODEX_PATH.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", CODEX_PATH] + args
    return [CODEX_PATH] + args


async def _run_codex_management_command(
    args: list[str],
    work_dir: str,
    *,
    timeout: float = 120.0,
) -> tuple[int, str, str]:
    kwargs = dict(
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
    )
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(*_codex_command(args), **kwargs)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _terminate_process_tree(proc)
        return -1, "", f"명령 시간이 {int(timeout)}초를 초과했습니다."
    return (
        int(proc.returncode or 0),
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


def format_codex_doctor_report(data: dict) -> str:
    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, dict):
        return "Codex doctor 결과 형식을 해석하지 못했습니다."
    counts = {"ok": 0, "warning": 0, "fail": 0}
    issues = []
    for check_id, check in checks.items():
        if not isinstance(check, dict):
            continue
        status = str(check.get("status", "unknown")).lower()
        if status in counts:
            counts[status] += 1
        if status in ("warning", "fail"):
            icon = "❌" if status == "fail" else "⚠️"
            summary = str(check.get("summary") or "상세 없음")
            issues.append(f"{icon} `{check_id}` — {summary}")
    overall = str(data.get("overallStatus") or "unknown").upper()
    version = str(data.get("codexVersion") or "unknown")
    lines = [
        f"🩺 **Codex Doctor** · `{overall}` · CLI `{version}`",
        f"✅ {counts['ok']} · ⚠️ {counts['warning']} · ❌ {counts['fail']}",
    ]
    lines.extend(issues[:12] or ["문제가 발견되지 않았습니다."])
    if len(issues) > 12:
        lines.append(f"… 추가 문제 {len(issues) - 12}개")
    return "\n".join(lines)


async def run_codex_doctor(work_dir: str) -> tuple[bool, str]:
    try:
        returncode, stdout, stderr = await _run_codex_management_command(
            ["doctor", "--json"], work_dir,
        )
    except FileNotFoundError:
        return False, "Codex CLI를 찾을 수 없습니다."
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        detail = stderr or stdout or f"returncode={returncode}"
        return False, f"Codex doctor 실행 결과를 해석하지 못했습니다:\n```\n{detail[-1500:]}\n```"
    # doctor는 진단 항목이 fail이면 비정상 코드를 돌려줄 수 있으므로 JSON을 우선한다.
    return True, format_codex_doctor_report(data)


def _thread_id_from_jsonl(text: str) -> str | None:
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id.strip():
                return thread_id.strip()
    return None


async def fork_codex_channel_session(channel_id: int, work_dir: str) -> tuple[bool, str]:
    session_id = channel_codex_sessions.get(channel_id)
    if not session_id:
        return False, "현재 채널에 fork할 Codex 세션이 없습니다."
    if channel_busy.get(channel_id):
        return False, "현재 작업이 끝난 뒤 다시 시도해주세요."
    args = ["exec", "fork", session_id] + _codex_exec_base_args(channel_id)
    try:
        returncode, stdout, stderr = await _run_codex_management_command(args, work_dir)
    except FileNotFoundError:
        return False, "Codex CLI를 찾을 수 없습니다."
    new_session_id = _thread_id_from_jsonl(stdout)
    if returncode != 0 or not new_session_id:
        detail = stderr or stdout or f"returncode={returncode}"
        return False, f"세션 fork 실패:\n```\n{detail[-1500:]}\n```"
    channel_codex_sessions[channel_id] = new_session_id
    save_codex_sessions()
    return True, f"🌿 세션을 분기했습니다.\n이전: `{session_id}`\n현재: `{new_session_id}`"


async def archive_codex_channel_session(channel_id: int, work_dir: str) -> tuple[bool, str]:
    session_id = channel_codex_sessions.get(channel_id)
    if not session_id:
        return False, "현재 채널에 보관할 Codex 세션이 없습니다."
    if channel_busy.get(channel_id):
        return False, "현재 작업이 끝난 뒤 다시 시도해주세요."
    try:
        returncode, stdout, stderr = await _run_codex_management_command(
            ["archive", session_id], work_dir,
        )
    except FileNotFoundError:
        return False, "Codex CLI를 찾을 수 없습니다."
    if returncode != 0:
        detail = stderr or stdout or f"returncode={returncode}"
        return False, f"세션 보관 실패:\n```\n{detail[-1500:]}\n```"
    channel_codex_sessions.pop(channel_id, None)
    save_codex_sessions()
    return True, f"📦 세션 `{session_id}`을 보관하고 채널 연결을 해제했습니다."


def _codex_model_options() -> list[dict]:
    """Codex CLI가 캐시한 현재 계정의 표시 가능 모델을 선택지로 변환한다."""
    data = _read_codex_models_cache()

    options = []
    seen = set()
    models = data.get("models", []) if isinstance(data, dict) else []
    for item in models:
        if not isinstance(item, dict) or item.get("visibility") != "list":
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        slug = slug.strip()
        key = slug.lower()
        if key in seen:
            continue
        seen.add(key)
        display_name = item.get("display_name")
        description = item.get("description")
        options.append({
            "value": slug,
            "label": display_name.strip() if isinstance(display_name, str) and display_name.strip() else slug,
            "backend": "codex",
            "model": slug,
            "description": (
                description.strip()
                if isinstance(description, str) and description.strip()
                else "Codex 모델"
            ),
            "priority": item.get("priority", 9999),
        })

    options.sort(key=lambda option: (
        option["priority"] if isinstance(option["priority"], int) else 9999,
        option["label"].lower(),
    ))

    if not options:
        fallback = _resolve_codex_model()
        if fallback:
            options.append({
                "value": fallback,
                "label": fallback,
                "backend": "codex",
                "model": fallback,
                "description": "현재 Codex 기본 모델 (캐시 목록 없음)",
                "priority": 0,
            })
    return options


def _all_select_model_options() -> list[dict]:
    """Codex 기본값, 계정별 Codex 모델, Claude 모델을 합친다."""
    return [SELECT_MODEL_OPTIONS[0], *_codex_model_options(), *SELECT_MODEL_OPTIONS[1:]]


def _find_select_model_option(name: str) -> dict | None:
    raw = name.strip()
    key = raw.lower()
    key = SELECT_MODEL_ALIASES.get(key, key)
    for option in _all_select_model_options():
        if key == option["value"].lower() or key == option["label"].lower():
            return option
    return None


def _select_model_choices(current: str = "") -> list[app_commands.Choice]:
    cur = current.lower().strip()
    choices = []
    for option in _all_select_model_options():
        default_model = (
            f" ({_resolve_codex_model()})"
            if option["backend"] == "codex" and option["model"] is None
            else ""
        )
        label = f"{option['label']}{default_model} - {option['description']}"
        haystack = f"{option['value']} {option['label']} {option['description']}".lower()
        if cur and cur not in haystack:
            continue
        choices.append(app_commands.Choice(name=label[:100], value=option["value"][:100]))
    return choices[:25]


def _model_display(channel_id: int) -> str:
    backend = channel_backends.get(channel_id, DEFAULT_BACKEND)
    if backend == "codex":
        return f"{_resolve_codex_model(channel_id) or '(default)'} (codex)"
    return f"{channel_models.get(channel_id, '(default)')} (claude)"


async def apply_selected_model(channel_id: int, name: str) -> tuple[bool, str]:
    option = _find_select_model_option(name)
    if not option:
        available = " / ".join(o["value"] for o in _all_select_model_options())
        return False, f"잘못된 모델 선택: `{name}` (사용 가능: {available})"

    backend = option["backend"]
    model = option["model"]
    previous_backend = channel_backends.get(channel_id, DEFAULT_BACKEND)
    reasoning_reset = ""

    channel_backends[channel_id] = backend
    save_backends()

    if backend == "codex":
        if model:
            channel_codex_models[channel_id] = model
        else:
            channel_codex_models.pop(channel_id, None)
        save_codex_models()

        selected_reasoning = channel_reasoning.get(channel_id)
        selected_effort = _normalize_reasoning_effort(selected_reasoning) if selected_reasoning else ""
        resolved_model = _resolve_codex_model(channel_id)
        supported = [level["effort"] for level in _codex_supported_reasoning(resolved_model)]
        if selected_effort and supported and selected_effort not in supported:
            channel_reasoning.pop(channel_id, None)
            save_codex_reasoning()
            reasoning_reset = (
                f"\n⚠️ `{resolved_model}`은 `{selected_effort}`를 지원하지 않아 "
                f"추론 강도를 `{_resolve_codex_reasoning(channel_id)}` 기본값으로 해제했습니다."
            )
    else:
        if model:
            channel_models[channel_id] = model
        else:
            channel_models.pop(channel_id, None)
        save_models()

    if previous_backend == "claude" or backend == "claude":
        await kill_channel_proc(channel_id)

    if backend == "codex":
        return True, (
            f"🤖 모델 선택: `codex` / `{_resolve_codex_model(channel_id)}` "
            f"(다음 메시지부터 적용){reasoning_reset}"
        )
    return True, f"🤖 모델 선택: `claude` / `{model}` (다음 메시지부터 적용)"


CODEX_PATH = _resolve_codex_path()


class CodexUsageLimitError(RuntimeError):
    def __init__(self, message: str, *, retry_at: datetime, thread_id: str | None = None):
        super().__init__(message)
        self.retry_at = retry_at
        self.thread_id = thread_id


def _resolve_codex_app_server_path() -> str | None:
    override = os.getenv("CODEX_APP_SERVER_PATH", "").strip()
    if override and os.path.isfile(override):
        return override

    if CODEX_PATH.lower().endswith(".exe") and os.path.isfile(CODEX_PATH):
        return CODEX_PATH

    launcher = os.path.abspath(CODEX_PATH) if os.path.dirname(CODEX_PATH) else ""
    base_dir = os.path.dirname(launcher) if launcher else os.path.expanduser(r"~\AppData\Roaming\npm")
    patterns = [
        os.path.join(
            base_dir, "node_modules", "@openai", "codex", "node_modules", "@openai",
            "codex-win32-*", "vendor", "*", "bin", "codex.exe",
        ),
        os.path.join(
            base_dir, "node_modules", "@openai", "codex", "vendor", "*", "bin", "codex.exe",
        ),
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return matches[0]
    return None


def _is_codex_usage_limit_error(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in (
        "you've hit your usage limit",
        "you have hit your usage limit",
        "usage limit reached",
        "usage_limit_reached",
        "rate_limit_reached",
    ))


def _is_codex_missing_thread_error(text: str) -> bool:
    """실제로 재개할 Codex 스레드가 없을 때만 저장된 세션을 폐기한다."""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in (
        "no conversation found",
        "thread not found",
        "thread was not found",
        "no rollout found for thread",
        "could not find thread",
        "unknown thread id",
    ))


def _parse_codex_usage_reset_at(text: str, *, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    source = str(text or "")

    relative = re.search(
        r"try again in\s+(\d+)\s*(second|minute|hour)s?",
        source,
        flags=re.IGNORECASE,
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        if unit == "second":
            return now + timedelta(seconds=amount)
        if unit == "minute":
            return now + timedelta(minutes=amount)
        return now + timedelta(hours=amount)

    match = re.search(
        r"try again at\s+([^\r\n]+?)(?:\.\s*$|$)",
        source,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if not match:
        return None

    value = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", match.group(1), flags=re.IGNORECASE)
    value = value.strip().rstrip(".")
    for fmt in (
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _usage_token_value(usage: dict, field: str) -> int:
    value = usage.get(field, 0) if isinstance(usage, dict) else 0
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _usage_total_tokens(usage: dict) -> int:
    # cached_input_tokens는 input_tokens의 부분집합이므로 중복 합산하지 않는다.
    return _usage_token_value(usage, "input_tokens") + _usage_token_value(
        usage, "output_tokens"
    )


def record_codex_channel_usage(
    channel_id: int,
    usage: dict,
    *,
    now: datetime | None = None,
) -> dict:
    """Codex 턴 사용량을 날짜/채널별로 누적하고 현재 채널 비중을 반환한다."""
    now = now or datetime.now()
    day_key = now.strftime("%Y-%m-%d")
    days = codex_usage_stats.setdefault("days", {})
    if not isinstance(days, dict):
        days = {}
        codex_usage_stats["days"] = days

    day = days.setdefault(day_key, {"channels": {}})
    if not isinstance(day, dict):
        day = {"channels": {}}
        days[day_key] = day
    channels = day.setdefault("channels", {})
    if not isinstance(channels, dict):
        channels = {}
        day["channels"] = channels

    channel_key = str(channel_id)
    totals = channels.setdefault(channel_key, {})
    if not isinstance(totals, dict):
        totals = {}
        channels[channel_key] = totals
    for field in CODEX_USAGE_TOKEN_FIELDS:
        totals[field] = _usage_token_value(totals, field) + _usage_token_value(usage, field)
    totals["turns"] = _usage_token_value(totals, "turns") + 1

    cutoff = (now - timedelta(days=CODEX_USAGE_RETENTION_DAYS - 1)).date()
    for stored_day in list(days):
        try:
            stored_date = datetime.strptime(stored_day, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            del days[stored_day]
            continue
        if stored_date < cutoff:
            del days[stored_day]

    save_codex_usage_stats()

    channel_total = _usage_total_tokens(totals)
    all_total = sum(
        _usage_total_tokens(channel_usage)
        for channel_usage in channels.values()
        if isinstance(channel_usage, dict)
    )
    share_percent = (channel_total / all_total * 100.0) if all_total else 0.0
    return {
        "date": day_key,
        "channel": dict(totals),
        "channel_total_tokens": channel_total,
        "all_channels_total_tokens": all_total,
        "share_percent": share_percent,
    }


def _compact_token_count(value: int) -> str:
    value = max(0, int(value or 0))
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{value:,}"


def _codex_rate_limit_state_from_result(result: dict, *, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    buckets = result.get("rateLimitsByLimitId") if isinstance(result, dict) else None
    if not isinstance(buckets, dict) or not buckets:
        single = result.get("rateLimits") if isinstance(result, dict) else None
        buckets = {"codex": single} if isinstance(single, dict) else {}

    blocked: list[dict] = []
    normalized_windows: list[dict] = []
    for limit_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        reached_type = bucket.get("rateLimitReachedType")
        windows = []
        for window_name in ("primary", "secondary"):
            window = bucket.get(window_name)
            if not isinstance(window, dict):
                continue
            used = window.get("usedPercent")
            reset_ts = window.get("resetsAt")
            windows.append({"name": window_name, "used": used, "reset_ts": reset_ts})
            if isinstance(used, (int, float)):
                normalized_windows.append({
                    "limit_id": str(limit_id),
                    "limit_name": bucket.get("limitName"),
                    "window_name": window_name,
                    "used_percent": float(used),
                    "remaining_percent": max(0.0, 100.0 - float(used)),
                    "window_duration_mins": window.get("windowDurationMins"),
                    "resets_at": (
                        datetime.fromtimestamp(float(reset_ts))
                        if isinstance(reset_ts, (int, float))
                        else None
                    ),
                })

        selected = []
        if reached_type in ("primary", "secondary"):
            selected = [window for window in windows if window["name"] == reached_type]
        if not selected:
            selected = [
                window for window in windows
                if isinstance(window.get("used"), (int, float)) and window["used"] >= 100
            ]
        if reached_type and not selected:
            selected = windows
        if not reached_type and not selected:
            continue

        reset_times = [
            datetime.fromtimestamp(float(window["reset_ts"]))
            for window in selected
            if isinstance(window.get("reset_ts"), (int, float))
        ]
        retry_at = max(reset_times) if reset_times else now + timedelta(minutes=CODEX_DEFERRED_FALLBACK_RETRY_MINUTES)
        retry_at += timedelta(seconds=CODEX_LIMIT_RETRY_GRACE_SECONDS)
        blocked.append({
            "limit_id": str(limit_id),
            "reached_type": reached_type,
            "retry_at": retry_at,
            "used_percent": max(
                [float(window["used"]) for window in selected if isinstance(window.get("used"), (int, float))]
                or [100.0]
            ),
        })

    if not blocked:
        return {
            "blocked": False,
            "retry_at": None,
            "limit_id": None,
            "reached_type": None,
            "windows": normalized_windows,
        }
    latest = max(blocked, key=lambda item: item["retry_at"])
    return {"blocked": True, **latest, "windows": normalized_windows}


async def _read_codex_rate_limit_state() -> dict | None:
    binary = _resolve_codex_app_server_path()
    if not binary:
        logger.warning("Codex App Server 실행 파일을 찾지 못해 오류 문구 기반 재시각을 사용합니다.")
        return None

    kwargs = dict(
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=os.path.dirname(__file__),
        limit=4 * 1024 * 1024,
    )
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "app-server", "--listen", "stdio://", **kwargs,
        )

        async def read_response(wanted_id: int) -> dict:
            async def _read_loop():
                while True:
                    raw = await _read_jsonl_line(proc.stdout)
                    if not raw:
                        raise RuntimeError("Codex App Server stdout closed")
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if message.get("id") == wanted_id:
                        return message
            return await asyncio.wait_for(_read_loop(), timeout=10)

        initialize = {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "discord_codex_supervisor",
                    "title": "Discord Codex Supervisor",
                    "version": "1.0.0",
                }
            },
        }
        proc.stdin.write((json.dumps(initialize) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        initialized = await read_response(1)
        if initialized.get("error"):
            raise RuntimeError(str(initialized["error"]))

        for message in (
            {"method": "initialized", "params": {}},
            {"method": "account/rateLimits/read", "id": 2, "params": {}},
        ):
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await proc.stdin.drain()

        response = await read_response(2)
        if response.get("error"):
            raise RuntimeError(str(response["error"]))
        result = response.get("result")
        if not isinstance(result, dict):
            return None
        return _codex_rate_limit_state_from_result(result)
    except Exception as e:
        logger.warning(f"Codex 사용량 조회 실패, 오류 문구 기반으로 대체: {e}")
        return None
    finally:
        if proc and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=2)
            except Exception:
                pass


async def _read_cached_codex_rate_limit_state() -> dict | None:
    """완료 메시지 표시용 계정 한도를 캐시해 App Server 조회를 줄인다."""
    now = time.monotonic()
    if now < float(codex_rate_limit_cache.get("expires_at", 0.0)):
        return codex_rate_limit_cache.get("state")

    state = await _read_codex_rate_limit_state()
    ttl = (
        CODEX_RATE_LIMIT_CACHE_SECONDS
        if state is not None
        else CODEX_RATE_LIMIT_FAILURE_CACHE_SECONDS
    )
    codex_rate_limit_cache["state"] = state
    codex_rate_limit_cache["expires_at"] = now + ttl
    return state


def _format_limit_duration(minutes) -> str:
    if not isinstance(minutes, (int, float)) or minutes <= 0:
        return "한도"
    minutes = int(minutes)
    if minutes % (7 * 24 * 60) == 0:
        return f"{minutes // (7 * 24 * 60)}주"
    if minutes % (24 * 60) == 0:
        return f"{minutes // (24 * 60)}일"
    if minutes % 60 == 0:
        return f"{minutes // 60}시간"
    return f"{minutes}분"


def _format_remaining_percent(value) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.1f}".rstrip("0").rstrip(".")


def _format_limit_reset(value, *, now: datetime | None = None) -> str:
    if not isinstance(value, datetime):
        return "초기화 미상"
    now = now or datetime.now()
    if value.date() == now.date():
        return value.strftime("%H:%M")
    return value.strftime("%m/%d %H:%M")


def _format_codex_rate_limit_line(state: dict | None, *, now: datetime | None = None) -> str | None:
    windows = state.get("windows") if isinstance(state, dict) else None
    if not isinstance(windows, list) or not windows:
        return None

    preferred = [
        window for window in windows
        if isinstance(window, dict) and window.get("limit_id") == "codex"
    ]
    selected = preferred or [window for window in windows if isinstance(window, dict)]
    selected = sorted(
        selected,
        key=lambda window: (
            window.get("window_duration_mins")
            if isinstance(window.get("window_duration_mins"), (int, float))
            else float("inf")
        ),
    )[:2]

    parts = []
    for window in selected:
        duration = _format_limit_duration(window.get("window_duration_mins"))
        remaining = _format_remaining_percent(window.get("remaining_percent"))
        reset = _format_limit_reset(window.get("resets_at"), now=now)
        parts.append(f"{duration} {remaining}% 남음({reset})")
    return "⏳ 한도: " + " · ".join(parts) if parts else None


def format_codex_usage_message(
    usage: dict,
    channel_daily: dict | None,
    rate_limit_state: dict | None,
    *,
    now: datetime | None = None,
) -> str:
    input_tokens = _usage_token_value(usage, "input_tokens")
    cached_tokens = _usage_token_value(usage, "cached_input_tokens")
    output_tokens = _usage_token_value(usage, "output_tokens")
    reasoning_tokens = _usage_token_value(usage, "reasoning_output_tokens")

    turn_parts = [
        f"입력 {_compact_token_count(input_tokens)}",
        f"캐시 {_compact_token_count(cached_tokens)}",
        f"출력 {_compact_token_count(output_tokens)}",
    ]
    if reasoning_tokens:
        turn_parts.append(f"추론 {_compact_token_count(reasoning_tokens)}")
    lines = ["📊 이번: " + " · ".join(turn_parts)]

    if isinstance(channel_daily, dict):
        channel_total = _compact_token_count(channel_daily.get("channel_total_tokens", 0))
        share = float(channel_daily.get("share_percent", 0.0) or 0.0)
        share_text = f"{share:.1f}".rstrip("0").rstrip(".")
        turns = _usage_token_value(channel_daily.get("channel", {}), "turns")
        lines.append(
            f"📈 이 채널 오늘: {channel_total} · 전체 {share_text}% · {turns}회"
        )

    limit_line = _format_codex_rate_limit_line(rate_limit_state, now=now)
    if limit_line:
        lines.append(limit_line)
    return "\n".join(lines)


async def _resolve_codex_usage_retry_at(error_text: str) -> datetime:
    state = await _read_codex_rate_limit_state()
    if state and state.get("blocked") and isinstance(state.get("retry_at"), datetime):
        return state["retry_at"]
    parsed = _parse_codex_usage_reset_at(error_text)
    if parsed:
        return parsed + timedelta(seconds=CODEX_LIMIT_RETRY_GRACE_SECONDS)
    return datetime.now() + timedelta(minutes=CODEX_DEFERRED_FALLBACK_RETRY_MINUTES)


DEFAULT_PERMISSIONS = ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)", "Glob(*)", "Grep(*)"]

# 이미지 첨부파일 저장 디렉토리
ATTACH_DIR = os.path.join(os.path.dirname(__file__), "attachments")
os.makedirs(ATTACH_DIR, exist_ok=True)

ATTACH_MAX_AGE = 24 * 60 * 60  # 첨부파일 보관 기간 (24시간)
ATTACH_CLEANUP_INTERVAL = 24 * 60 * 60  # 정리 주기 (24시간)

# 재시작 감지용 플래그 파일
_RESTART_FLAG_FILE = os.path.join(os.path.dirname(__file__), ".bot_running")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
TEXT_EXTS = {".txt", ".md", ".json", ".log", ".csv", ".xml", ".yaml", ".yml",
             ".py", ".js", ".ts", ".cs", ".java", ".cpp", ".c", ".h", ".bat", ".sh"}
ALLOWED_EXTS = IMAGE_EXTS | TEXT_EXTS
SCREENSHOT_KEYWORDS = (
    "스샷", "스크린샷", "캡처", "캡쳐", "화면 찍", "화면 캡", "screenshot",
    "screen shot", "capture", "snapshot",
)
IMAGE_ATTACHMENT_KEYWORDS = SCREENSHOT_KEYWORDS + (
    "png", ".png", "jpg", ".jpg", "jpeg", ".jpeg", "webp", ".webp",
    "gif", ".gif", "bmp", ".bmp", "이미지", "그림", "사진",
)
SCREENSHOT_NAME_HINTS = (
    "screenshot", "screen-shot", "screen_shot", "capture", "snapshot",
    "preview", "result", "캡처", "캡쳐", "스샷", "스크린샷",
)
AUTO_ATTACH_IMAGE_LIMIT = 5
AUTO_ATTACH_SCAN_LIMIT = 5000
AUTO_ATTACH_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", "__pycache__", "node_modules",
    ".venv", "venv", "env", "Library", "Temp", "obj", "bin", "dist", "build",
    "attachments",
}


async def cleanup_attachments():
    """오래된 첨부파일 삭제 (ATTACH_MAX_AGE 초과)"""
    import time as _time
    now = _time.time()
    count = 0
    protected = {
        os.path.normcase(os.path.abspath(str(path)))
        for job in codex_deferred_jobs.values()
        for key in ("image_paths", "queued_image_paths")
        for path in (job.get(key) or [])
        if path
    }
    try:
        for fname in os.listdir(ATTACH_DIR):
            fpath = os.path.join(ATTACH_DIR, fname)
            if os.path.normcase(os.path.abspath(fpath)) in protected:
                continue
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > ATTACH_MAX_AGE:
                os.remove(fpath)
                count += 1
    except Exception as e:
        logger.error(f"첨부파일 정리 중 오류: {e}")
    if count:
        logger.info(f"오래된 첨부파일 {count}개 삭제")


async def _attachment_cleanup_loop():
    """주기적으로 오래된 첨부파일을 정리하는 백그라운드 루프"""
    while True:
        await asyncio.sleep(ATTACH_CLEANUP_INTERVAL)
        await cleanup_attachments()


async def download_attachments(attachments: list[discord.Attachment]) -> list[str]:
    """Discord 첨부파일(이미지+텍스트)을 로컬에 다운로드하고 경로 리스트 반환"""
    paths = []
    async with aiohttp.ClientSession() as session:
        for att in attachments:
            ext = os.path.splitext(att.filename)[1].lower()
            if ext not in ALLOWED_EXTS:
                continue
            local_path = os.path.join(ATTACH_DIR, f"{att.id}_{att.filename}")
            async with session.get(att.url) as resp:
                if resp.status == 200:
                    with open(local_path, "wb") as f:
                        f.write(await resp.read())
                    paths.append(local_path)
    return paths


def wants_screenshot_attachment(prompt: str) -> bool:
    text = prompt.lower()
    return any(keyword in text for keyword in IMAGE_ATTACHMENT_KEYWORDS)


def wants_screenshot_capture(prompt: str) -> bool:
    text = prompt.lower()
    return any(keyword in text for keyword in SCREENSHOT_KEYWORDS)


def add_screenshot_output_instruction(prompt: str, work_dir: str) -> str:
    if not wants_screenshot_capture(prompt):
        return prompt
    out_dir = os.path.join(work_dir, "codex_screenshots")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        out_dir = work_dir
    filename = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    out_path = os.path.join(out_dir, filename)
    rel_path = os.path.relpath(out_path, work_dir).replace("\\", "/")
    return (
        f"{prompt}\n\n"
        "[Discord bot screenshot request]\n"
        f"- The user requested a screenshot/capture. If you produce a visual result or inspect a browser/app UI, save a PNG screenshot to `{rel_path}` under the current working directory.\n"
        "- Prefer Playwright/browser screenshot APIs when available. If the environment cannot capture a screenshot, explain why briefly.\n"
        "- After saving, mention the screenshot file path in your final response."
    )


def normalize_reported_image_path(raw_path: str, work_dir: str) -> str:
    path = raw_path.strip().strip("`'\"<>[]()")
    path = path.replace("/", os.sep)

    if os.name == "nt":
        if re.match(rf"^{re.escape(os.sep)}[A-Za-z]:{re.escape(os.sep)}", path):
            path = path[1:]
        elif path.lower().startswith(f"{os.sep}users{os.sep}"):
            path = f"C:{path}"
    elif path.startswith(f"~{os.sep}"):
        path = os.path.expanduser(path)
    if path.startswith(f"~{os.sep}"):
        path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(work_dir, path)

    return os.path.normpath(path)


def extract_image_paths_from_text(text: str, work_dir: str) -> list[str]:
    if not text:
        return []

    ext_pattern = r"(?:png|jpg|jpeg|gif|webp|bmp)"
    patterns = [
        # Markdown 로컬 파일 링크: [미리보기](C:/.../result.png)
        rf"\]\(\s*<?([^<>\r\n)]+?\.{ext_pattern})>?(?:#[^)]*)?\s*\)",
        # 코드 표시된 절대/상대 경로 및 파일명: `codex_screenshots/result.png`
        rf"`([^`\r\n]+?\.{ext_pattern})`",
        rf"<([^<>]+?\.{ext_pattern})>",
        rf"((?:[A-Za-z]:[\\/]|/Users/|~/|\.\.?[\\/])[^`'\"<>\s)]+?\.{ext_pattern})",
        # 작업 폴더 기준 상대 경로: codex_screenshots/result.png
        rf"((?:[A-Za-z0-9_.-]+[\\/])+[^`'\"<>\s)]+?\.{ext_pattern})",
        # 작업 폴더 바로 아래의 단순 파일명: result.png
        rf"(?<![A-Za-z0-9_.\\/-])([A-Za-z0-9_.-]+\.{ext_pattern})(?![A-Za-z0-9_.-])",
    ]
    found: list[str] = []
    seen: set[str] = set()

    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            raw = match.group(1)
            path = normalize_reported_image_path(raw, work_dir)
            if path in seen or not os.path.isfile(path):
                continue
            seen.add(path)
            found.append(path)

    return found


def collect_recent_images(root: str, since_ts: float, limit: int = AUTO_ATTACH_IMAGE_LIMIT) -> list[str]:
    if not root or not os.path.isdir(root):
        return []

    root = os.path.abspath(root)
    candidates: list[tuple[int, float, str]] = []
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in AUTO_ATTACH_SKIP_DIRS and not d.startswith(".")
        ]
        for filename in filenames:
            scanned += 1
            if scanned > AUTO_ATTACH_SCAN_LIMIT:
                break
            ext = os.path.splitext(filename)[1].lower()
            if ext not in IMAGE_EXTS:
                continue
            path = os.path.join(dirpath, filename)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if stat.st_size <= 0 or stat.st_mtime < since_ts:
                continue
            name = filename.lower()
            score = 1 if any(hint in name for hint in SCREENSHOT_NAME_HINTS) else 0
            candidates.append((score, stat.st_mtime, os.path.normpath(path)))
        if scanned > AUTO_ATTACH_SCAN_LIMIT:
            break

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [path for _, _, path in candidates[:limit]]


async def send_files_to_channel(channel, paths: list[str], *, title: str = "📎 파일"):
    valid: list[str] = []
    skipped: list[str] = []
    limit_bytes = getattr(getattr(channel, "guild", None), "filesize_limit", 25 * 1024 * 1024) or (25 * 1024 * 1024)

    for path in paths:
        if not os.path.isfile(path):
            skipped.append(f"`{os.path.basename(path)}` (없음)")
            continue
        size = os.path.getsize(path)
        if size == 0:
            skipped.append(f"`{os.path.basename(path)}` (빈 파일)")
            continue
        if size > limit_bytes:
            skipped.append(f"`{os.path.basename(path)}` ({size/1024/1024:.1f}MB, 크기 초과)")
            continue
        valid.append(path)

    for i in range(0, len(valid), 10):
        chunk = valid[i:i + 10]
        files = [discord.File(path, filename=os.path.basename(path)) for path in chunk]
        names = ", ".join(f"`{os.path.basename(path)}`" for path in chunk)
        await channel.send(f"{title} {names}", files=files)

    if skipped:
        await channel.send("⚠️ 첨부 제외: " + ", ".join(skipped))


async def send_screenshot_outputs_if_requested(channel, prompt: str, work_dir: str, since_ts: float, result_text: str = ""):
    requested = wants_screenshot_attachment(prompt)
    reported_images = extract_image_paths_from_text(result_text, work_dir)

    # 사용자가 이미지 첨부를 명시하지 않았더라도 최종 답변이 실제 이미지
    # 파일을 보고하면 Discord에 전달한다. 단순한 "PNG 형식" 언급은
    # extract_image_paths_from_text()의 파일 존재 검사에서 제외된다.
    if not requested and not reported_images:
        return

    candidates = list(reported_images)
    if requested:
        candidates.extend(collect_recent_images(work_dir, since_ts))

    images = []
    for path in candidates:
        if path not in images:
            images.append(path)
    if not images:
        if requested:
            await channel.send("📷 새로 생성된 스크린샷 파일을 찾지 못했습니다.")
        return
    await send_files_to_channel(channel, images, title="📷 스크린샷")

def ensure_claude_settings(work_dir: str):
    """작업 디렉토리에 .claude/settings.json이 없거나 permissions가 없으면 추가"""
    claude_dir = os.path.join(work_dir, ".claude")
    settings_file = os.path.join(claude_dir, "settings.json")

    os.makedirs(claude_dir, exist_ok=True)

    settings = {}
    if os.path.exists(settings_file):
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            settings = {}

    permissions = settings.setdefault("permissions", {})
    existing = set(permissions.get("allow", []))
    merged = list(existing | set(DEFAULT_PERMISSIONS))
    permissions["allow"] = merged

    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)

# Discord 메시지 글자 제한
MAX_MSG_LEN = 1900

def split_message(text: str, limit: int = MAX_MSG_LEN) -> list[str]:
    """긴 메시지를 Discord 글자 제한에 맞게 분할"""
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # 코드블록 중간에 끊기지 않도록 줄바꿈 기준으로 자름
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def send_codex_result_to_channel(
    channel,
    result_text: str,
    reply_to: discord.Message | None = None,
):
    """Codex 최종 답변을 Discord Markdown으로 전송한다."""
    for i, chunk in enumerate(split_message(result_text)):
        if not chunk:
            continue
        send_kwargs = {
            # 코드블록 외부로 출력하더라도 답변 속 문자열이 실제 멘션을
            # 발생시키지 않도록 한다.
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if reply_to and i == 0:
            send_kwargs["reference"] = reply_to
        await channel.send(chunk, **send_kwargs)


def create_bot() -> commands.Bot:
    """새 봇 인스턴스를 생성하고 이벤트/명령어를 등록"""
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    _register_events(bot)
    return bot


TOOL_ICONS = {
    "Read": "📖", "Write": "✏️", "Edit": "✏️", "Bash": "⚡",
    "Glob": "🔍", "Grep": "🔍", "TodoWrite": "📋", "WebSearch": "🌐",
    "WebFetch": "🌐", "AskUserQuestion": "❓",
}


async def _stderr_reader(channel_id: int, proc: asyncio.subprocess.Process, buffer: list):
    """프로세스 stderr를 읽어 로그에 남기고 buffer에 누적 (returncode!=0 진단용)"""
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            logger.warning(f"채널 {channel_id} stderr: {text}")
            buffer.append(text)
            if len(buffer) > 200:
                del buffer[:len(buffer) - 200]
            # resume 실패 자동 복구: 만료된 session_id 제거
            if "No conversation found" in text and channel_id in channel_sessions:
                stale = channel_sessions.pop(channel_id, None)
                save_sessions()
                logger.warning(f"채널 {channel_id}: 만료 session_id 폐기 ({stale}), 다음 spawn은 fresh start")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"채널 {channel_id} stderr reader 종료: {e}")


async def _stdout_reader(channel_id: int, proc: asyncio.subprocess.Process, queue: asyncio.Queue):
    """프로세스 stdout을 지속적으로 읽어 JSON 이벤트로 파싱 후 queue에 전달. EOF 시 sentinel 전송."""
    try:
        while True:
            try:
                line = await _read_jsonl_line(proc.stdout)
            except JsonlLineTooLargeError as error:
                logger.warning(f"채널 {channel_id}: Claude JSONL 초대형 줄 건너뜀: {error}")
                continue
            if not line:
                await queue.put({"__eof__": True})
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            await queue.put(data)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"stdout reader 오류 (channel {channel_id}): {e}", exc_info=True)
        try:
            await queue.put({"__eof__": True, "__error__": str(e)})
        except Exception:
            pass


async def spawn_persistent_proc(channel_id: int, work_dir: str) -> dict:
    """채널용 장기 실행 Claude 프로세스 생성. stream-json 입출력으로 여러 턴 처리."""
    cmd = [
        CLAUDE_PATH,
        "--dangerously-skip-permissions",
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--add-dir", ATTACH_DIR,
        "--add-dir", os.path.expanduser("~/.claude"),
    ]

    session_id = channel_sessions.get(channel_id)
    if session_id:
        cmd += ["--resume", session_id]

    model = channel_models.get(channel_id)
    if model:
        cmd += ["--model", model]

    logger.info(f"Claude persistent 프로세스 시작: cwd={work_dir}, channel={channel_id}, resume={session_id}, model={model or '(default)'}")

    env = os.environ.copy()
    # Unity 빌드용 환경변수 — .env에 설정된 경우만 전달
    for key in ("ANDROID_HOME", "JAVA_HOME"):
        val = os.getenv(key)
        if val:
            env[key] = val

    kwargs = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=work_dir,
        env=env,
    )
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    proc = await asyncio.create_subprocess_exec(*cmd, limit=1024*1024, **kwargs)
    event_queue: asyncio.Queue = asyncio.Queue()
    reader_task = asyncio.create_task(_stdout_reader(channel_id, proc, event_queue))
    stderr_buffer: list[str] = []
    stderr_task = asyncio.create_task(_stderr_reader(channel_id, proc, stderr_buffer))

    import time as _time
    session = {
        "proc": proc,
        "work_dir": work_dir,
        "reader_task": reader_task,
        "stderr_task": stderr_task,
        "stderr_buffer": stderr_buffer,
        "event_queue": event_queue,
        "last_active": _time.time(),
        "session_id": session_id,
        "lock": asyncio.Lock(),
    }
    channel_claude_procs[channel_id] = session
    return session


async def kill_channel_proc(channel_id: int):
    """채널 프로세스 종료 및 리소스 정리"""
    session = channel_claude_procs.pop(channel_id, None)
    if not session:
        return
    proc = session["proc"]
    reader_task = session["reader_task"]
    stderr_task = session.get("stderr_task")
    stderr_buffer = session.get("stderr_buffer") or []
    try:
        proc.kill()
    except Exception:
        pass
    try:
        reader_task.cancel()
    except Exception:
        pass
    if stderr_task:
        try:
            stderr_task.cancel()
        except Exception:
            pass
    try:
        if proc.stderr:
            proc.stderr.close()
    except Exception:
        pass
    try:
        await proc.wait()
    except Exception:
        pass
    rc = proc.returncode
    if rc not in (None, 0) and stderr_buffer:
        tail = "\n".join(stderr_buffer[-10:])
        logger.error(f"채널 {channel_id}: 프로세스 종료 (returncode={rc}) stderr tail:\n{tail}")
    else:
        logger.info(f"채널 {channel_id}: 프로세스 종료 (returncode={rc})")


async def _terminate_process_tree(proc, *, timeout: float = 8.0):
    """Codex 실행 프로세스와 그 아래 MCP/셸 자식까지 종료한다."""
    if not proc:
        return

    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP로 시작한 Codex에는 먼저 Ctrl+Break를 보내
        # 정상 정리 기회를 준다. 지원하지 않거나 응답하지 않으면 기존의
        # taskkill /T /F로 확실하게 프로세스 트리를 정리한다.
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            await asyncio.wait_for(proc.wait(), timeout=min(1.5, timeout))
            return
        except (AttributeError, asyncio.TimeoutError, ProcessLookupError, OSError):
            pass
        # Codex가 cmd -> node -> codex.exe -> MCP 순으로 자식을 만들기 때문에
        # proc.kill()만으로는 실제 작업이 고아 프로세스로 남는다.
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await asyncio.wait_for(killer.wait(), timeout=timeout)
        except (FileNotFoundError, asyncio.TimeoutError, ProcessLookupError, OSError):
            pass

    if getattr(proc, "returncode", None) is None:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass


async def _stop_channel_codex_run(channel_id: int) -> bool:
    """추적 중인 Codex 프로세스 트리와 진행 상태 갱신을 즉시 정리한다."""
    run = channel_codex_runs.get(channel_id)
    if not run:
        return False

    status_task = run.get("status_task")
    if status_task and not status_task.done():
        status_task.cancel()
        await asyncio.gather(status_task, return_exceptions=True)

    await _terminate_process_tree(run.get("proc"))

    status_msg = run.get("status_msg")
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    channel_status.pop(channel_id, None)
    if channel_codex_runs.get(channel_id) is run:
        channel_codex_runs.pop(channel_id, None)
    return True


async def get_or_spawn_proc(channel_id: int, work_dir: str) -> dict:
    """기존 세션 반환 또는 신규 생성. 프로세스 죽었거나 work_dir 변경 시 재생성."""
    session = channel_claude_procs.get(channel_id)
    if session:
        proc = session["proc"]
        if proc.returncode is not None:
            logger.warning(f"채널 {channel_id}: 기존 프로세스 종료됨 (returncode={proc.returncode}), 재생성")
            await kill_channel_proc(channel_id)
            session = None
        elif session["work_dir"] != work_dir:
            logger.info(f"채널 {channel_id}: work_dir 변경, 재생성 ({session['work_dir']} → {work_dir})")
            await kill_channel_proc(channel_id)
            session = None

    if session is None:
        session = await spawn_persistent_proc(channel_id, work_dir)
    return session


async def send_user_message(session: dict, content: str):
    """Claude 프로세스에 user 메시지 JSON 라인 전송. 전송 전 이전 턴 잔여 이벤트 드레인."""
    queue: asyncio.Queue = session["event_queue"]
    drained = 0
    while not queue.empty():
        try:
            queue.get_nowait()
            drained += 1
        except asyncio.QueueEmpty:
            break
    if drained:
        logger.warning(f"send_user_message: 이전 턴 잔여 이벤트 {drained}개 드레인")

    proc = session["proc"]
    msg = {"type": "user", "message": {"role": "user", "content": content}}
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    proc.stdin.write(line.encode("utf-8"))
    await proc.stdin.drain()


MAX_RETRIES = 2

CODEX_PROGRESS_MAX_ENTRIES = 7
CODEX_PROGRESS_REFRESH_SECONDS = 2.0
CODEX_PROGRESS_HEARTBEAT_SECONDS = 30.0
CODEX_COMPLETED_EXIT_GRACE_SECONDS = 1.0


def _format_codex_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}분 {secs:02d}초"
    return f"{secs}초"


class _CodexProgressPanel:
    """Codex JSON 이벤트를 Discord에 보여줄 최근 작업 목록으로 압축한다."""

    def __init__(self, max_entries: int = CODEX_PROGRESS_MAX_ENTRIES, started_at: float | None = None):
        self.max_entries = max(1, max_entries)
        self.started_at = time.monotonic() if started_at is None else started_at
        self.entries: list[dict] = []
        self.hidden_count = 0

    def upsert(self, key: str, text: str, *, completed: bool = False) -> bool:
        clean = " ".join(str(text).replace("@", "@\u200b").split()).strip()
        if not clean:
            return False

        existing_index = next(
            (i for i, entry in enumerate(self.entries) if entry["key"] == key),
            None,
        )
        if existing_index is not None:
            existing = self.entries.pop(existing_index)
            if existing["text"] == clean and existing["completed"] == completed:
                self.entries.insert(existing_index, existing)
                return False
        elif self.entries and self.entries[-1]["text"] == clean:
            return False

        self.entries.append({"key": key, "text": clean[:180], "completed": completed})
        while len(self.entries) > self.max_entries:
            self.entries.pop(0)
            self.hidden_count += 1
        return True

    def render(self, now: float | None = None) -> str:
        current = time.monotonic() if now is None else now
        elapsed = _format_codex_elapsed(current - self.started_at)
        lines = [f"⏳ **Codex 작업 중** · {elapsed}"]
        lines.extend(f"• {entry['text']}" for entry in self.entries)
        if self.hidden_count:
            lines.append(f"… 이전 작업 {self.hidden_count}개 정리됨")
        return "\n".join(lines)[:1900]

async def run_codex(prompt: str, work_dir: str, channel_id: int = 0,
                    image_paths: list[str] | None = None,
                    status_channel: discord.TextChannel | None = None,
                    reply_to: discord.Message | None = None) -> str:
    """codex CLI exec 모드 실행. --json 출력 파싱 + 세션(thread_id) 자동 재개."""

    status_msg = None
    if status_channel:
        try:
            status_msg = await status_channel.send("🔧 Codex 실행 중...")
        except Exception:
            pass

    # 채널 세션 있으면 resume
    resume_id = channel_codex_sessions.get(channel_id) if channel_id else None

    # exec resume은 -C/--cd 옵션을 안 받음 → subprocess cwd로 통일
    # 전역 config.toml을 읽어 설치된 플러그인(Chrome 포함)을 모든 채널에 로드한다.
    # 연결되지 않은 localhost HTTP MCP만 _codex_exec_base_args에서 이번 턴에 끈다.
    # 채널별 모델/추론 강도는 -m / -c로 계속 명시 적용한다.
    base_args = _codex_exec_base_args(channel_id)
    if resume_id:
        cmd_args = ["exec", "resume", resume_id] + base_args
    else:
        cmd_args = ["exec"] + base_args

    # 이미지 첨부 (codex는 -i 직접 지원)
    if image_paths:
        for img in image_paths:
            if os.path.splitext(img)[1].lower() in IMAGE_EXTS:
                cmd_args += ["-i", img]
    # 긴 프롬프트/개행/따옴표가 Windows command line에서 깨지지 않도록 stdin으로 전달한다.
    cmd_args.append("-")

    # Windows에서 npm shim(.cmd/.bat)은 직접 exec 안 됨 → cmd.exe /c 로 감쌈
    if os.name == "nt" and CODEX_PATH.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/c", CODEX_PATH] + cmd_args
    else:
        cmd = [CODEX_PATH] + cmd_args

    logger.info(
        f"Codex 실행: cwd={work_dir}, channel={channel_id}, resume={resume_id}, "
        f"prompt_len={len(prompt)}, prompt={prompt[:80]}..."
    )

    kwargs = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=work_dir,
    )
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    try:
        proc = await asyncio.create_subprocess_exec(*cmd, limit=1024*1024, **kwargs)
    except FileNotFoundError:
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
        return "오류 발생: codex CLI를 찾을 수 없습니다. `npm i -g @openai/codex` 후 PATH 확인."

    run_state = {
        "proc": proc,
        "status_task": None,
        "status_msg": status_msg,
    }
    if channel_id:
        channel_codex_runs[channel_id] = run_state

    def _short_status_text(value, limit: int = 100) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            value = " ".join(str(part) for part in value)
        elif isinstance(value, dict):
            simple = []
            for key, val in value.items():
                if isinstance(val, (str, int, float, bool)) and val not in ("", None):
                    simple.append(f"{key}={val}")
                if len(simple) >= 3:
                    break
            value = ", ".join(simple)
        text = str(value).replace("\r", " ").replace("\n", " ").replace("`", "'").strip()
        return text[:limit]

    def _find_nested_value(obj, keys: tuple[str, ...]):
        if isinstance(obj, dict):
            for key in keys:
                if key in obj and obj[key] not in ("", None):
                    return obj[key]
            for val in obj.values():
                found = _find_nested_value(val, keys)
                if found not in ("", None):
                    return found
        elif isinstance(obj, list):
            for val in obj:
                found = _find_nested_value(val, keys)
                if found not in ("", None):
                    return found
        return None

    def _codex_item_detail(item: dict) -> str:
        command = _find_nested_value(item, ("command", "cmd", "shell_command"))
        if command:
            return f"`{_short_status_text(command, 120)}`"

        path = _find_nested_value(item, ("file_path", "path", "cwd"))
        if path:
            return f"`{os.path.basename(str(path)) or _short_status_text(path, 80)}`"

        name = _find_nested_value(item, ("name", "tool_name", "tool", "function"))
        args = _find_nested_value(item, ("arguments", "args", "input"))
        if name and args:
            return f"`{_short_status_text(name, 40)}` {_short_status_text(args, 80)}"
        if name:
            return f"`{_short_status_text(name, 80)}`"

        query = _find_nested_value(item, ("query", "pattern"))
        if query:
            return f"`{_short_status_text(query, 100)}`"

        simple = []
        for key, val in item.items():
            if key in ("id", "type") or val in ("", None):
                continue
            if isinstance(val, (str, int, float, bool)):
                simple.append(f"{key}={val}")
            if len(simple) >= 2:
                break
        return _short_status_text(", ".join(simple), 100)

    def _codex_progress_event(ev: dict) -> tuple[str, str, bool] | None:
        t = ev.get("type", "")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        item_type = str(item.get("type") or ev.get("item_type") or "").lower()
        detail = _codex_item_detail(item) if item else ""
        suffix = f" {detail}" if detail else ""

        if t == "thread.started":
            return "launch", "✅ Codex 세션 준비 완료", True
        if t == "turn.started":
            return "turn", "🧠 요청 분석 중...", False
        if t == "turn.completed":
            return "turn", "✅ 요청 처리 완료", True
        if t == "turn.failed":
            return "turn", "⚠️ 요청 처리 실패", True
        if t == "error":
            return "error", "⚠️ 오류 내용을 확인 중...", False

        if t not in ("item.started", "item.updated", "item.completed"):
            return None

        done = t == "item.completed"
        failed = str(item.get("status") or "").lower() in ("failed", "error")
        prefix = "⚠️" if failed else ("✅" if done else "🔧")
        ending = "실패" if failed else ("완료" if done else "중...")
        item_id = item.get("id") or ev.get("item_id") or f"{item_type}:{detail}"
        key = f"item:{item_id}"

        if item_type in ("agent_message", "message"):
            preview = _short_status_text(item.get("text"), 140)
            text = f"💬 {preview}" if preview else "💬 답변 구성 중..."
            return key, text, done
        if "reason" in item_type:
            text = "🧠 해결 방법 검토 중..." if not done else "✅ 해결 방법 검토 완료"
            return key, text, done
        if "file" in item_type or "patch" in item_type or "edit" in item_type:
            return key, f"{prefix} 파일 수정 {ending}{suffix}", done
        if "web" in item_type or "search" in item_type:
            return key, f"{prefix} 자료 검색 {ending}{suffix}", done
        if "todo" in item_type or "plan" in item_type:
            return key, f"{prefix} 작업 계획 갱신 {ending}", done
        if "shell" in item_type or "command" in item_type or "exec" in item_type:
            command = item.get("command") or item.get("cmd") or item.get("text") or ""
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            command = _short_status_text(command, 100)
            command_suffix = f" `{command}`" if command else suffix
            return key, f"{prefix} 명령 실행 {ending}{command_suffix}", done
        if "mcp" in item_type or "tool" in item_type or "call" in item_type:
            name = item.get("name") or item.get("tool_name") or item.get("tool") or item_type
            name = _short_status_text(name, 80)
            return key, f"{prefix} 도구 `{name}` 사용 {ending}", done

        label = _short_status_text((item_type or "작업").replace("_", " "), 80)
        return key, f"{prefix} `{label}` 처리 {ending}{suffix}", done

    progress_panel = _CodexProgressPanel()
    progress_panel.upsert("launch", "🚀 Codex에 요청 전달", completed=True)
    status_changed = asyncio.Event()
    status_closed = False
    status_pump_task: asyncio.Task | None = None
    last_status_edit = 0.0

    async def _render_codex_status():
        nonlocal status_msg, last_status_edit
        if not status_channel or status_closed:
            return
        text = progress_panel.render()
        try:
            if status_msg:
                await status_msg.edit(content=text[:1900])
            else:
                status_msg = await status_channel.send(text[:1900])
                run_state["status_msg"] = status_msg
            channel_status[channel_id] = text
            last_status_edit = time.monotonic()
        except Exception:
            pass

    async def _codex_status_pump():
        while not status_closed:
            try:
                await asyncio.wait_for(
                    status_changed.wait(),
                    timeout=CODEX_PROGRESS_HEARTBEAT_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
            status_changed.clear()
            if status_closed:
                break
            delay = CODEX_PROGRESS_REFRESH_SECONDS - (time.monotonic() - last_status_edit)
            if delay > 0:
                await asyncio.sleep(delay)
            if status_closed:
                break
            await _render_codex_status()

    def _queue_codex_progress(update: tuple[str, str, bool] | None):
        if not update:
            return
        key, text, completed = update
        if progress_panel.upsert(key, text, completed=completed):
            status_changed.set()

    async def _remove_codex_status():
        nonlocal status_closed, status_msg
        status_closed = True
        status_changed.set()
        if status_pump_task:
            status_pump_task.cancel()
            try:
                await status_pump_task
            except asyncio.CancelledError:
                pass
        channel_status.pop(channel_id, None)
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
            status_msg = None
        run_state["status_task"] = None
        run_state["status_msg"] = None
        if channel_id and channel_codex_runs.get(channel_id) is run_state:
            channel_codex_runs.pop(channel_id, None)

    if status_channel:
        status_pump_task = asyncio.create_task(_codex_status_pump())
        run_state["status_task"] = status_pump_task
        status_changed.set()

    stderr_task = asyncio.create_task(proc.stderr.read())

    # JSONL 파싱
    agent_messages: list[str] = []
    new_thread_id: str | None = None
    usage_info: dict | None = None
    error_messages: list[str] = []
    cancelled = False
    turn_completed = False
    try:
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionError, OSError):
            pass

        while True:
            # turn.completed가 오기 전까지는 장시간 도구 실행도 끝까지 기다린다.
            # 완료 뒤에는 stdout EOF를 기다리지 않는다. 하위 프로세스가 파이프를
            # 붙잡고 있으면 작업은 끝났는데 Discord 상태만 계속 남을 수 있기 때문이다.
            try:
                raw_line = await _read_jsonl_line(proc.stdout)
            except JsonlLineTooLargeError as error:
                logger.warning(f"채널 {channel_id}: Codex JSONL 초대형 줄 건너뜀: {error}")
                error_messages.append(str(error))
                _queue_codex_progress(("jsonl:error", "⚠️ 초대형 도구 출력 1건 건너뜀", True))
                continue

            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            _queue_codex_progress(_codex_progress_event(ev))

            t = ev.get("type", "")
            if t == "thread.started":
                new_thread_id = ev.get("thread_id")
            elif t == "item.completed":
                item = ev.get("item", {})
                if item.get("type") == "agent_message":
                    txt = item.get("text", "")
                    if txt:
                        agent_messages.append(txt)
            elif t == "error":
                msg = ev.get("message", "")
                if msg:
                    error_messages.append(str(msg))
            elif t == "turn.failed":
                err = ev.get("error", {})
                msg = err.get("message") if isinstance(err, dict) else str(err)
                if msg:
                    error_messages.append(str(msg))
            elif t == "turn.completed":
                usage_info = ev.get("usage")
                turn_completed = True
                logger.info(f"채널 {channel_id}: Codex turn.completed 수신, 결과 확정")
                break
    except asyncio.CancelledError:
        cancelled = True
        await _terminate_process_tree(proc)
        if not stderr_task.done():
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        await _remove_codex_status()
        logger.info(f"채널 {channel_id}: Codex 실행 취소 완료 (세션 유지: {resume_id or new_thread_id})")
        raise
    except Exception:
        await _terminate_process_tree(proc)
        if not stderr_task.done():
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        await _remove_codex_status()
        raise
    finally:
        if proc.returncode is None:
            if cancelled:
                await _terminate_process_tree(proc)
            elif turn_completed:
                # 정상 종료할 짧은 여유만 준 뒤 남은 MCP/셸 자식까지 정리한다.
                try:
                    await asyncio.wait_for(
                        proc.wait(), timeout=CODEX_COMPLETED_EXIT_GRACE_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        f"채널 {channel_id}: 완료 후 Codex 프로세스가 남아 있어 정리"
                    )
                    await _terminate_process_tree(proc)
            else:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    await _terminate_process_tree(proc)

    try:
        stderr = await asyncio.wait_for(stderr_task, timeout=2)
    except Exception:
        stderr = b""

    # 세션 ID 저장 (새 thread 시작 시에만 갱신)
    if new_thread_id and channel_id and not resume_id:
        channel_codex_sessions[channel_id] = new_thread_id
        save_codex_sessions()

    # exec JSON은 중간 commentary와 최종 답변을 모두 agent_message로 보낸다.
    # 진행 중 commentary는 패널에 보여주고, 완료 후에는 마지막 결론만 남긴다.
    result_text = agent_messages[-1].strip() if agent_messages else ""

    # turn.completed 이후 잔여 프로세스를 강제 정리한 종료 코드는 실패가 아니다.
    if not turn_completed and proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        combined_error = "\n".join(part for part in (
            stderr_text,
            "\n".join(error_messages),
        ) if part).strip()
        err_tail = stderr_text[-500:]
        if not err_tail and error_messages:
            err_tail = "\n".join(error_messages)[-1000:]
        logger.warning(f"Codex returncode={proc.returncode} stderr: {err_tail}")

        if _is_codex_usage_limit_error(combined_error):
            retry_at = await _resolve_codex_usage_retry_at(combined_error)
            thread_id = resume_id or new_thread_id
            if thread_id and channel_id:
                channel_codex_sessions[channel_id] = thread_id
                save_codex_sessions()
            await _remove_codex_status()
            raise CodexUsageLimitError(
                err_tail or "Codex 사용량 제한에 도달했습니다.",
                retry_at=retry_at,
                thread_id=thread_id,
            )

        # 강제 중지·일시적 CLI 오류로 맥락을 잃지 않도록, 실제로 스레드가
        # 존재하지 않는다는 오류가 확인된 경우에만 fresh start로 전환한다.
        if resume_id and _is_codex_missing_thread_error(combined_error):
            channel_codex_sessions.pop(channel_id, None)
            save_codex_sessions()
            logger.warning(f"채널 {channel_id}: 존재하지 않는 codex thread_id 폐기")
        if not result_text:
            await _remove_codex_status()
            return f"오류 발생: Codex 종료 (returncode={proc.returncode})\n{err_tail}"

    await _remove_codex_status()

    if not result_text:
        return "(빈 응답)"

    channel_daily_usage = None
    if usage_info and channel_id:
        try:
            channel_daily_usage = record_codex_channel_usage(channel_id, usage_info)
        except Exception as e:
            logger.warning(f"채널 {channel_id}: Codex 사용량 누적 저장 실패: {e}")

    if status_channel:
        try:
            await send_codex_result_to_channel(
                status_channel,
                result_text,
                reply_to=reply_to,
            )
        except Exception:
            pass
        # 이미 완료 이벤트에 포함된 턴 사용량과 5분 캐시한 계정 한도를
        # 기존 통계 메시지 하나로 함께 표시한다.
        if usage_info:
            try:
                rate_limit_state = await _read_cached_codex_rate_limit_state()
                stats = format_codex_usage_message(
                    usage_info,
                    channel_daily_usage,
                    rate_limit_state,
                )
                await status_channel.send(
                    stats,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as e:
                logger.warning(f"채널 {channel_id}: Codex 사용량 표시 실패: {e}")

    return result_text


async def run_claude(prompt: str, work_dir: str, channel_id: int = 0,
                     image_paths: list[str] | None = None,
                     status_channel: discord.TextChannel | None = None,
                     reply_to: discord.Message | None = None) -> str:
    """claude CLI를 대화형 모드로 실행. 스트리밍으로 실시간 응답 표시. API 에러 시 자동 재시도."""

    # 결과 시작부/특정 문구만 매칭 (일반 답변에 "500", "529" 등이 섞여도 오탐 안 나도록)
    def _is_error(res: str) -> bool:
        if not res:
            return True
        if res.startswith("⏰") or res.startswith("오류 발생") or res.startswith("claude CLI를 찾을 수 없습니다"):
            return True
        for p in ("API Error", "Stream idle timeout", "Overloaded"):
            if p in res:
                return True
        return False

    for attempt in range(MAX_RETRIES + 1):
        result = await _run_claude_once(prompt, work_dir, channel_id, image_paths, status_channel, reply_to)
        if not _is_error(result):
            return result
        if attempt < MAX_RETRIES:
            logger.warning(f"Claude API 오류, 재시도 {attempt + 1}/{MAX_RETRIES}: {result[:100]}")
            if status_channel:
                try:
                    await status_channel.send(f"⚠️ 응답 실패, 재시도 중... ({attempt + 1}/{MAX_RETRIES})")
                except Exception:
                    pass
            await asyncio.sleep(2)
    return result


async def _run_claude_once(prompt: str, work_dir: str, channel_id: int = 0,
                           image_paths: list[str] | None = None,
                           status_channel: discord.TextChannel | None = None,
                           reply_to: discord.Message | None = None) -> str:
    """run_claude의 단일 실행. persistent 프로세스에서 턴 단위로 실행."""

    import time as _time

    # 이번 턴에서 Claude가 작업한 파일 추적 시작 (이전 턴 결과는 폐기)
    if channel_id:
        channel_recent_files[channel_id] = []

    # spawn이 필요한지 미리 판단해서, 필요하면 "준비 중" 메시지를 즉시 띄운다
    existing = channel_claude_procs.get(channel_id)
    needs_spawn = (
        existing is None
        or existing["proc"].returncode is not None
        or existing["work_dir"] != work_dir
    )
    pre_spawn_msg = None
    if needs_spawn and status_channel:
        try:
            if channel_sessions.get(channel_id):
                pre_spawn_msg = await status_channel.send("🔧 Claude 세션 복원 중... (수 초~수십 초 소요)")
            else:
                pre_spawn_msg = await status_channel.send("🔧 Claude 프로세스 시작 중...")
        except Exception:
            pass

    try:
        session = await get_or_spawn_proc(channel_id, work_dir)
    except FileNotFoundError:
        if pre_spawn_msg:
            try:
                await pre_spawn_msg.delete()
            except Exception:
                pass
        return f"claude CLI를 찾을 수 없습니다: {CLAUDE_PATH}"
    except Exception as e:
        if pre_spawn_msg:
            try:
                await pre_spawn_msg.delete()
            except Exception:
                pass
        logger.error(f"프로세스 생성 실패: {e}", exc_info=True)
        return f"오류 발생: {e}"

    session["last_active"] = _time.time()

    try:
        async with session["lock"]:
            # user 메시지 전송 (stdin 실패 시 재생성 후 1회 재시도)
            try:
                await send_user_message(session, prompt)
            except (BrokenPipeError, ConnectionError, OSError) as e:
                logger.warning(f"채널 {channel_id}: stdin write 실패 ({e}), 프로세스 재시작")
                await kill_channel_proc(channel_id)
                session = await spawn_persistent_proc(channel_id, work_dir)
                await send_user_message(session, prompt)

            event_queue: asyncio.Queue = session["event_queue"]

            new_session_id = None
            status_msg = None          # 도구 상태 표시용 메시지
            stream_msg = None          # 스트리밍 텍스트 표시용 메시지
            stream_msgs = []           # 스트리밍 메시지 목록 (분할 시)
            stream_buf = ""            # 텍스트 누적 버퍼
            last_edit_time = 0         # 마지막 메시지 edit 시각 (rate limit 방지)
            EDIT_INTERVAL = 1.0        # 최소 edit 간격 (초)
            result_text = ""
            is_thinking = False        # thinking 블록 진행 중 여부

            if status_channel:
                if pre_spawn_msg:
                    try:
                        await pre_spawn_msg.edit(content="⏳ Claude 처리 중...")
                        status_msg = pre_spawn_msg
                    except Exception:
                        try:
                            status_msg = await status_channel.send("⏳ Claude 처리 중...")
                        except Exception:
                            pass
                else:
                    try:
                        status_msg = await status_channel.send("⏳ Claude 처리 중...")
                    except Exception:
                        pass

            async def _flush_stream():
                """버퍼에 쌓인 텍스트를 Discord에 반영"""
                nonlocal stream_msg, stream_buf, stream_msgs, last_edit_time
                if not stream_buf or not status_channel:
                    return

                now = asyncio.get_event_loop().time()
                if now - last_edit_time < EDIT_INTERVAL:
                    return

                display = stream_buf
                if len(display) > MAX_MSG_LEN - 10:
                    cut = stream_buf[:MAX_MSG_LEN - 10]
                    remainder = stream_buf[MAX_MSG_LEN - 10:]
                    try:
                        if stream_msg:
                            await stream_msg.edit(content=f"```\n{cut}\n```")
                        else:
                            send_kwargs = {}
                            if reply_to:
                                send_kwargs["reference"] = reply_to
                            stream_msg = await status_channel.send(f"```\n{cut}\n```", **send_kwargs)
                        stream_msgs.append(stream_msg)
                    except Exception:
                        pass
                    stream_msg = None
                    stream_buf = remainder
                    display = remainder

                try:
                    if stream_msg:
                        await stream_msg.edit(content=f"```\n{display}\n```")
                    elif display.strip():
                        send_kwargs = {}
                        if reply_to and not stream_msgs:
                            send_kwargs["reference"] = reply_to
                        stream_msg = await status_channel.send(f"```\n{display}\n```", **send_kwargs)
                except Exception:
                    pass
                last_edit_time = now

            while True:
                # 장시간 도구 실행도 끝까지 기다린다. 사용자는 `중지` 명령으로 직접 취소할 수 있다.
                data = await event_queue.get()

                # 프로세스 EOF (예기치 않은 종료)
                if data.get("__eof__"):
                    err = data.get("__error__", "프로세스 종료")
                    logger.warning(f"채널 {channel_id}: {err}")
                    await kill_channel_proc(channel_id)
                    channel_status.pop(channel_id, None)
                    if status_msg:
                        try:
                            await status_msg.delete()
                        except Exception:
                            pass
                    return f"오류 발생: 프로세스 종료됨 ({err})"

                msg_type = data.get("type", "")

                if msg_type == "system":
                    continue

                # stream_event: 토큰 단위 스트리밍 (--include-partial-messages)
                if msg_type == "stream_event" and status_channel:
                    event = data.get("event", {})
                    event_type = event.get("type", "")

                    if event_type == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                is_thinking = False
                                if status_msg:
                                    try:
                                        await status_msg.delete()
                                        status_msg = None
                                    except Exception:
                                        pass
                                stream_buf += text
                                await _flush_stream()
                        elif delta.get("type") == "thinking_delta":
                            if not is_thinking:
                                is_thinking = True
                                status = "🤔 thinking..."
                                if channel_id:
                                    channel_status[channel_id] = status
                                try:
                                    if status_msg:
                                        await status_msg.edit(content=status)
                                    else:
                                        status_msg = await status_channel.send(status)
                                except Exception:
                                    pass

                    elif event_type == "content_block_start":
                        block = event.get("content_block", {})
                        if block.get("type") == "thinking":
                            is_thinking = True
                            status = "🤔 thinking..."
                            if channel_id:
                                channel_status[channel_id] = status
                            try:
                                if status_msg:
                                    await status_msg.edit(content=status)
                                else:
                                    status_msg = await status_channel.send(status)
                            except Exception:
                                pass
                        elif block.get("type") == "tool_use":
                            is_thinking = False
                            tool_name = block.get("name", "")
                            icon = TOOL_ICONS.get(tool_name, "🔧")
                            status = f"{icon} `{tool_name}` ..."
                            await _flush_stream()
                            if channel_id:
                                channel_status[channel_id] = status
                            try:
                                if status_msg:
                                    await status_msg.edit(content=status)
                                else:
                                    status_msg = await status_channel.send(status)
                            except Exception:
                                pass

                    elif event_type == "content_block_stop":
                        if is_thinking:
                            is_thinking = False

                    continue

                # assistant: 완성된 메시지 (도구 상세 정보, AskUserQuestion 감지용)
                if msg_type == "assistant" and status_channel:
                    for content in data.get("message", {}).get("content", []):
                        if content.get("type") == "tool_use" and content.get("name") == "AskUserQuestion":
                            tool_input = content.get("input", {})
                            # 신형 schema: input.questions[] / 구형: input.question
                            questions = tool_input.get("questions") or (
                                [{"question": tool_input["question"]}] if tool_input.get("question") else []
                            )
                            lines = []
                            for q in questions:
                                q_text = q.get("question", "").strip()
                                header = q.get("header", "").strip()
                                if header:
                                    lines.append(f"**[{header}]** {q_text}")
                                elif q_text:
                                    lines.append(f"**{q_text}**")
                                for i, opt in enumerate(q.get("options", []), 1):
                                    label = opt.get("label", "")
                                    desc = opt.get("description", "")
                                    line = f"  `{i}.` {label}"
                                    if desc:
                                        line += f" — {desc}"
                                    lines.append(line)
                            body = "\n".join(lines) if lines else "(질문 내용 없음)"
                            await _flush_stream()
                            try:
                                if status_msg:
                                    await status_msg.delete()
                                    status_msg = None
                                msg_text = f"❓ **Claude가 질문합니다:**\n{body}\n*(답변을 입력하면 다음 메시지로 전달됩니다)*"
                                # Discord 2000자 제한 대응
                                if len(msg_text) > 1900:
                                    msg_text = msg_text[:1900] + "\n... (잘림)"
                                await status_channel.send(msg_text)
                            except Exception:
                                pass
                            continue

                        if content.get("type") == "tool_use":
                            tool_name = content.get("name", "")
                            tool_input = content.get("input", {})
                            icon = TOOL_ICONS.get(tool_name, "🔧")

                            # /files 다운로드용 — Write/Edit/MultiEdit 파일 추적
                            if tool_name in ("Write", "Edit", "MultiEdit") and channel_id:
                                fp = tool_input.get("file_path", "")
                                if fp:
                                    abs_fp = fp if os.path.isabs(fp) else os.path.join(work_dir, fp)
                                    abs_fp = os.path.normpath(abs_fp)
                                    recent = channel_recent_files.setdefault(channel_id, [])
                                    if abs_fp not in recent:
                                        recent.append(abs_fp)

                            if tool_name in ("Read",):
                                detail = tool_input.get("file_path", "")
                                status = f"{icon} `{tool_name}` {os.path.basename(detail)}"
                            elif tool_name in ("Write", "Edit"):
                                detail = tool_input.get("file_path", "")
                                status = f"{icon} `{tool_name}` {os.path.basename(detail)}"
                            elif tool_name == "Bash":
                                detail = tool_input.get("command", "")[:60]
                                status = f"{icon} `{tool_name}` `{detail}`"
                            elif tool_name in ("Glob", "Grep"):
                                pattern = tool_input.get("pattern", "")
                                status = f"{icon} `{tool_name}` {pattern}"
                            else:
                                status = f"{icon} `{tool_name}`"

                            if channel_id:
                                channel_status[channel_id] = status
                            try:
                                if status_msg:
                                    await status_msg.edit(content=status)
                                else:
                                    status_msg = await status_channel.send(status)
                            except Exception:
                                pass

                # 턴 완료
                if msg_type == "result":
                    result_text = data.get("result", "")
                    new_session_id = data.get("session_id")

                    if status_channel:
                        stats_parts = []
                        cost = data.get("cost_usd")
                        if cost is not None:
                            stats_parts.append(f"💰 ${cost:.4f}")
                        total_cost = data.get("total_cost_usd")
                        if total_cost is not None:
                            stats_parts.append(f"(세션 ${total_cost:.4f})")
                        duration_ms = data.get("duration_ms")
                        if duration_ms is not None:
                            stats_parts.append(f"⏱️ {duration_ms / 1000:.1f}s")
                        num_turns = data.get("num_turns")
                        if num_turns is not None:
                            stats_parts.append(f"🔄 {num_turns}턴")
                        usage = data.get("usage", {})
                        input_tokens = usage.get("input_tokens", 0)
                        output_tokens = usage.get("output_tokens", 0)
                        if input_tokens or output_tokens:
                            stats_parts.append(f"📊 {input_tokens:,}→{output_tokens:,} tok")
                        if stats_parts:
                            try:
                                await status_channel.send(" ".join(stats_parts))
                            except Exception:
                                pass

                    break

                if msg_type == "rate_limit_event":
                    continue

            # 턴 후 상태 정리
            channel_status.pop(channel_id, None)
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

            # 스트리밍 메시지를 최종 result로 교체
            if result_text and status_channel:
                final_chunks = split_message(result_text)
                if stream_msg and stream_msg not in stream_msgs:
                    stream_msgs.append(stream_msg)

                for i, chunk in enumerate(final_chunks):
                    content = f"```\n{chunk}\n```"
                    if i < len(stream_msgs):
                        try:
                            await stream_msgs[i].edit(content=content)
                        except Exception:
                            pass
                    else:
                        try:
                            send_kwargs = {}
                            if reply_to and i == 0 and not stream_msgs:
                                send_kwargs["reference"] = reply_to
                            await status_channel.send(content, **send_kwargs)
                        except Exception:
                            pass

                for i in range(len(final_chunks), len(stream_msgs)):
                    try:
                        await stream_msgs[i].delete()
                    except Exception:
                        pass

            if new_session_id and channel_id:
                channel_sessions[channel_id] = new_session_id
                session["session_id"] = new_session_id
                save_sessions()

            session["last_active"] = _time.time()
            return result_text or "(빈 응답)"

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Claude 실행 오류: {e}", exc_info=True)
        channel_status.pop(channel_id, None)
        return f"오류 발생: {e}"


def _read_skill_frontmatter_value(skill_md_path: str, key: str) -> str:
    """SKILL.md frontmatter에서 단일 행 문자열 값을 추출한다."""
    try:
        with open(skill_md_path, "r", encoding="utf-8") as f:
            in_fm = False
            for line in f:
                stripped = line.rstrip()
                if stripped == "---":
                    if not in_fm:
                        in_fm = True
                        continue
                    return ""
                if in_fm and stripped.lower().startswith(f"{key.lower()}:"):
                    value = stripped.split(":", 1)[1].strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                        value = value[1:-1]
                    return value
    except Exception:
        pass
    return ""


def _read_skill_description(skill_md_path: str) -> str:
    return _read_skill_frontmatter_value(skill_md_path, "description")


def _scan_skill_dir(skill_dir: str) -> list[dict]:
    """단일 skills/ 디렉토리에서 (name, description) 추출"""
    if not os.path.isdir(skill_dir):
        return []
    out = []
    try:
        for entry in sorted(os.listdir(skill_dir), key=str.lower):
            ep = os.path.join(skill_dir, entry)
            skill_md = os.path.join(ep, "SKILL.md")
            if not os.path.isfile(skill_md):
                continue
            out.append({
                "name": _read_skill_frontmatter_value(skill_md, "name") or entry,
                "description": _read_skill_description(skill_md),
                "path": skill_md,
            })
    except Exception as e:
        logger.warning(f"스킬 디렉토리 스캔 실패 {skill_dir}: {e}")
    return out


def _scan_command_dir(cmd_dir: str) -> list[dict]:
    """플러그인 commands/ 디렉토리의 .md 파일에서 (name, description) 추출"""
    if not os.path.isdir(cmd_dir):
        return []
    out = []
    try:
        for entry in os.listdir(cmd_dir):
            if not entry.endswith(".md"):
                continue
            path = os.path.join(cmd_dir, entry)
            if not os.path.isfile(path):
                continue
            out.append({
                "name": entry[:-3],
                "description": _read_skill_description(path),
                "path": path,
            })
    except Exception as e:
        logger.warning(f"플러그인 commands 디렉토리 스캔 실패 {cmd_dir}: {e}")
    return out


def _scan_plugin_cache(cache_root: str, engine: str) -> list[tuple[list[dict], str, str]]:
    """플러그인 캐시 구조가 바뀌어도 모든 최상위 skills/commands를 찾는다."""
    if not os.path.isdir(cache_root):
        return []
    found: list[tuple[list[dict], str, str]] = []
    seen_roots: set[str] = set()
    try:
        candidates = sorted(
            glob.glob(os.path.join(cache_root, "**", "skills"), recursive=True),
            reverse=True,
        )
        for skills_dir in candidates:
            normalized = os.path.normcase(os.path.normpath(skills_dir))
            if normalized in seen_roots or not os.path.isdir(skills_dir):
                continue
            seen_roots.add(normalized)
            version_dir = os.path.dirname(skills_dir)
            plugin = os.path.basename(os.path.dirname(version_dir))
            items = _scan_skill_dir(skills_dir)
            if items and plugin:
                found.append((items, f"plugin:{plugin}", engine))

        command_candidates = sorted(
            glob.glob(os.path.join(cache_root, "**", "commands"), recursive=True),
            reverse=True,
        )
        for commands_dir in command_candidates:
            normalized = os.path.normcase(os.path.normpath(commands_dir))
            if normalized in seen_roots or not os.path.isdir(commands_dir):
                continue
            seen_roots.add(normalized)
            version_dir = os.path.dirname(commands_dir)
            plugin = os.path.basename(os.path.dirname(version_dir))
            items = _scan_command_dir(commands_dir)
            if items and plugin:
                found.append((items, f"plugin:{plugin}", engine))
    except Exception as e:
        logger.warning(f"플러그인 캐시 스캔 실패 {cache_root}: {e}")
    return found


BUILTIN_SLASH_COMMANDS = [
    ("init", "새 CLAUDE.md 생성 (코드베이스 문서화)"),
    ("review", "PR 리뷰"),
    ("security-review", "현재 브랜치 보안 리뷰"),
    ("update-config", "settings.json 설정 변경 (권한, hooks, env)"),
    ("simplify", "변경된 코드의 재사용/품질/효율 리뷰 + 수정"),
    ("loop", "프롬프트/슬래시 명령을 주기적으로 실행"),
    ("schedule", "원격 스케줄 에이전트 관리 (cron)"),
    ("claude-api", "Claude API / Anthropic SDK 앱 빌드/디버그/최적화"),
    ("keybindings-help", "키보드 단축키 커스터마이즈"),
    ("fewer-permission-prompts", "권한 프롬프트 줄이는 allowlist 추가"),
]


def scan_all_skills(work_dir: str | None = None) -> list[dict]:
    """Claude/Codex 프로젝트·글로벌 스킬과 기존 Claude 명령을 통합한다."""
    skills: list[dict] = []
    seen: set[str] = set()

    def add_items(items: list[dict], source: str, engine: str, prefix: str = ""):
        for item in items:
            name = f"{prefix}{item['name']}"
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            skills.append({
                **item,
                "name": name,
                "source": source,
                "engine": engine,
            })

    if work_dir:
        # Codex 프로젝트 스킬을 먼저 넣어 같은 이름의 Claude 복사본보다 우선한다.
        add_items(
            _scan_skill_dir(os.path.join(work_dir, ".agents", "skills")),
            "codex_project",
            "codex",
        )
        add_items(
            _scan_skill_dir(os.path.join(work_dir, ".codex", "skills")),
            "codex_project",
            "codex",
        )
        add_items(
            _scan_skill_dir(os.path.join(work_dir, ".claude", "skills")),
            "claude_project",
            "claude",
        )
        add_items(
            _scan_command_dir(os.path.join(work_dir, ".claude", "commands")),
            "claude_project",
            "claude",
        )

    codex_home = os.getenv("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    codex_skills = os.path.join(codex_home, "skills")
    add_items(_scan_skill_dir(codex_skills), "codex_global", "codex")
    add_items(
        _scan_skill_dir(os.path.join(codex_skills, ".system")),
        "codex_global",
        "codex",
    )

    add_items(
        _scan_skill_dir(os.path.expanduser("~/.claude/skills")),
        "claude_global",
        "claude",
    )
    add_items(
        _scan_command_dir(os.path.expanduser("~/.claude/commands")),
        "claude_global",
        "claude",
    )

    for cache_root, engine in (
        (os.path.expanduser("~/.codex/plugins/cache"), "codex"),
        (os.path.expanduser("~/.claude/plugins/cache"), "claude"),
    ):
        for items, source, item_engine in _scan_plugin_cache(cache_root, engine):
            plugin = source.split(":", 1)[1]
            add_items(items, source, item_engine, prefix=f"{plugin}:")

    for name, desc in BUILTIN_SLASH_COMMANDS:
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        skills.append({
            "name": name,
            "description": desc,
            "source": "builtin",
            "engine": "claude",
        })

    return skills


SKILL_SOURCE_ORDER = {
    "codex_project": 0,
    "claude_project": 1,
    "codex_global": 2,
    "claude_global": 3,
    "builtin": 5,
}


def _skill_autocomplete_choices(work_dir: str, current: str = "") -> list[app_commands.Choice]:
    skills = scan_all_skills(work_dir)
    query = current.casefold().strip()
    if query:
        skills = [
            skill for skill in skills
            if query in f"{skill['name']} {skill.get('description', '')}".casefold()
        ]
    skills.sort(key=lambda skill: (
        SKILL_SOURCE_ORDER.get(skill["source"], 4),
        skill["name"].casefold(),
    ))

    choices = []
    for skill in skills[:25]:
        source = skill["source"]
        if source == "codex_project":
            tag = "🤖📁"
        elif source == "claude_project":
            tag = "📁"
        elif source == "codex_global":
            tag = "🤖🌐"
        elif source == "claude_global":
            tag = "🌐"
        elif source == "builtin":
            tag = "⭐"
        else:
            tag = "🔌"
        label = f"{tag} {skill['name']}"
        if skill.get("description"):
            label = f"{label} — {skill['description']}"
        choices.append(app_commands.Choice(
            name=label[:100],
            value=skill["name"][:100],
        ))
    return choices


def _skill_invocation_prompt(channel_id: int, name: str, args: str = "") -> str:
    """활성 백엔드에 맞는 스킬 호출 문법으로 프롬프트를 만든다."""
    skill_name = name.strip().lstrip("/$")
    suffix = f" {args.strip()}" if args.strip() else ""
    backend = channel_backends.get(channel_id, DEFAULT_BACKEND)
    prefix = "$" if backend == "codex" else "/"
    return f"{prefix}{skill_name}{suffix}"


def is_claude_channel(channel: discord.abc.GuildChannel) -> bool:
    """해당 채널이 Claude 카테고리에 속하는지 확인"""
    return (
        CLAUDE_CATEGORY_ID
        and hasattr(channel, "category_id")
        and channel.category_id == CLAUDE_CATEGORY_ID
    )


async def _prepare_prompt(message: discord.Message) -> tuple[str, list[str]]:
    """메시지에서 프롬프트와 이미지 경로를 추출"""
    prompt = message.content.strip()
    image_paths = []
    if message.attachments:
        image_paths = await download_attachments(message.attachments)
    if image_paths:
        images = [p for p in image_paths if os.path.splitext(p)[1].lower() in IMAGE_EXTS]
        texts = [p for p in image_paths if os.path.splitext(p)[1].lower() in TEXT_EXTS]
        parts = []
        if images:
            refs = "\n".join(f"- {p.replace(os.sep, '/')}" for p in images)
            parts.append(f"[첨부 이미지 - 반드시 Read 도구로 아래 경로를 읽어서 이미지를 확인한 뒤 답변해줘]:\n{refs}")
        if texts:
            refs = "\n".join(f"- {p.replace(os.sep, '/')}" for p in texts)
            parts.append(f"[첨부 파일 - 반드시 Read 도구로 아래 경로를 읽어서 내용을 확인한 뒤 답변해줘]:\n{refs}")
        prompt = f"{prompt}\n\n" + "\n\n".join(parts)
    return prompt, image_paths


def _deferred_datetime_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _deferred_datetime(value, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return default or datetime.now()


def _append_deferred_request(channel_id: int, prompt: str, image_paths: list[str] | None = None) -> dict | None:
    job = codex_deferred_jobs.get(channel_id)
    if not job:
        return None
    clean_prompt = str(prompt or "").strip()
    if clean_prompt:
        job.setdefault("queued_prompts", []).append(clean_prompt)
    queued_images = job.setdefault("queued_image_paths", [])
    for path in image_paths or []:
        if path and path not in queued_images:
            queued_images.append(path)
    job["updated_at"] = _deferred_datetime_text(datetime.now())
    save_codex_deferred_jobs()
    return job


def _cancel_deferred_codex_job(channel_id: int) -> bool:
    removed = codex_deferred_jobs.pop(channel_id, None)
    if removed is not None:
        save_codex_deferred_jobs()
        logger.info(f"채널 {channel_id}: Codex 자동 재개 대기 작업 취소")
        return True
    return False


async def _park_codex_job(
    channel,
    channel_id: int,
    prompt: str,
    work_dir: str,
    image_paths: list[str] | None,
    error: CodexUsageLimitError,
    *,
    source: str,
    source_message_id: int | None = None,
    scheduled_job_id: str | None = None,
) -> dict:
    now = datetime.now()
    existing = codex_deferred_jobs.get(channel_id)
    if existing:
        _append_deferred_request(channel_id, prompt, image_paths)
        existing["retry_at"] = _deferred_datetime_text(error.retry_at)
        existing["thread_id"] = error.thread_id or existing.get("thread_id")
        existing["last_error"] = str(error)[-2000:]
        existing["updated_at"] = _deferred_datetime_text(now)
        save_codex_deferred_jobs()
        job = existing
    else:
        job = {
            "channel_id": channel_id,
            "source": source,
            "source_message_id": source_message_id,
            "scheduled_job_id": scheduled_job_id,
            "work_dir": work_dir,
            "prompt": str(prompt or "").strip(),
            "image_paths": list(dict.fromkeys(image_paths or [])),
            "queued_prompts": [],
            "queued_image_paths": [],
            "thread_id": error.thread_id,
            "retry_at": _deferred_datetime_text(error.retry_at),
            "created_at": _deferred_datetime_text(now),
            "updated_at": _deferred_datetime_text(now),
            "resume_attempts": 0,
            "non_quota_failures": 0,
            "last_error": str(error)[-2000:],
            "last_claude_audit_at": None,
        }
        codex_deferred_jobs[channel_id] = job
        save_codex_deferred_jobs()

    try:
        await channel.send(
            "⏸️ **Codex 사용량 제한으로 작업을 보관했습니다.**\n"
            f"자동 재개 예정: `{job['retry_at']}`\n"
            "봇을 재시작해도 유지되며, `중지` 또는 `리셋`으로 예약을 취소할 수 있습니다."
        )
    except Exception:
        pass
    logger.info(
        f"채널 {channel_id}: Codex 작업 자동 재개 대기 retry_at={job.get('retry_at')} "
        f"thread={job.get('thread_id')}"
    )
    return job


def _build_deferred_resume_prompt(job: dict, consumed_prompts: list[str]) -> str:
    original = str(job.get("prompt", "")).strip()
    additional = "\n\n".join(str(p).strip() for p in consumed_prompts if str(p).strip())
    parts = [
        "[Codex 사용량 제한 해제 후 자동 재개]",
        "이전 작업이 사용량 제한으로 중단되었습니다. 현재 파일과 작업 상태를 먼저 확인하고, "
        "이미 완료된 변경은 반복하지 말고 남은 작업부터 이어서 완료해 주세요.",
    ]
    if original:
        parts.append(f"원래 요청:\n{original}")
    if additional:
        parts.append(f"대기 중 추가 요청:\n{additional}")
    return "\n\n".join(parts)


async def _run_claude_readonly_audit(prompt: str, work_dir: str) -> str:
    args = [
        "-p",
        "--output-format", "json",
        "--tools", "Read,Glob,Grep",
    ]
    if os.name == "nt" and CLAUDE_PATH.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd.exe", "/c", CLAUDE_PATH] + args
    else:
        cmd = [CLAUDE_PATH] + args

    kwargs = dict(
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
    )
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
    try:
        stdout, stderr = await proc.communicate(prompt.encode("utf-8"))
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            return f"Claude 읽기 전용 감독 실패 (returncode={proc.returncode}): {stderr_text[-1000:]}"

        output = stdout.decode("utf-8", errors="replace").strip()
        try:
            data = json.loads(output)
            if isinstance(data, dict):
                result = data.get("result")
                if isinstance(result, str) and result.strip():
                    return result.strip()
        except json.JSONDecodeError:
            pass
        return output or "Claude 읽기 전용 감독 결과가 비어 있습니다."
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass


async def _run_deferred_claude_audit(channel, channel_id: int, job: dict, error_text: str):
    now = datetime.now()
    last_audit = _deferred_datetime(job.get("last_claude_audit_at"), default=datetime.min)
    if now - last_audit < timedelta(minutes=CODEX_CLAUDE_AUDIT_INTERVAL_MINUTES):
        return

    job["last_claude_audit_at"] = _deferred_datetime_text(now)
    save_codex_deferred_jobs()
    try:
        await channel.send(
            f"🕵️ Codex 자동 재개가 {job.get('non_quota_failures', 0)}회 실패해 "
            "Claude 감독 점검을 시작합니다."
        )
    except Exception:
        pass

    audit_prompt = (
        "[Codex 자동 재개 감독 점검]\n"
        "읽기 전용 진단입니다. 파일 수정, Write/Edit, 명령 실행, 외부 전송을 하지 마세요. "
        "현재 저장소 상태와 아래 실패 정보를 확인하여 가능한 원인과 사용자가 취할 조치만 간단히 보고하세요.\n\n"
        f"작업 폴더: {job.get('work_dir')}\n"
        f"재개 실패 횟수: {job.get('non_quota_failures', 0)}\n"
        f"마지막 오류: {str(error_text)[-1500:]}\n"
        f"원래 요청 요약: {str(job.get('prompt', ''))[:1000]}"
    )
    try:
        result = await _run_claude_readonly_audit(
            audit_prompt,
            str(job.get("work_dir") or WORKING_DIR),
        )
        for i, chunk in enumerate(split_message(result, limit=1800)):
            prefix = "🕵️ **Claude 감독 보고**\n" if i == 0 else ""
            await channel.send(prefix + chunk)
    except Exception as e:
        logger.warning(f"채널 {channel_id}: Claude 감독 점검 실패: {e}")


async def _run_deferred_codex_job(bot: commands.Bot, channel_id: int):
    job = codex_deferred_jobs.get(channel_id)
    if not job:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.warning(f"Codex 자동 재개 채널 조회 실패 ({channel_id}): {e}")
            job["retry_at"] = _deferred_datetime_text(
                datetime.now() + timedelta(minutes=CODEX_DEFERRED_BUSY_RETRY_MINUTES)
            )
            job.pop("running", None)
            job.pop("started_at", None)
            save_codex_deferred_jobs()
            return

    if channel_busy.get(channel_id):
        job["retry_at"] = _deferred_datetime_text(
            datetime.now() + timedelta(minutes=CODEX_DEFERRED_BUSY_RETRY_MINUTES)
        )
        job.pop("running", None)
        job.pop("started_at", None)
        save_codex_deferred_jobs()
        return

    state = await _read_codex_rate_limit_state()
    if state and state.get("blocked") and isinstance(state.get("retry_at"), datetime):
        job["retry_at"] = _deferred_datetime_text(state["retry_at"])
        job["updated_at"] = _deferred_datetime_text(datetime.now())
        job.pop("running", None)
        job.pop("started_at", None)
        save_codex_deferred_jobs()
        logger.info(f"채널 {channel_id}: Codex 제한 지속, 재개 연기 {job['retry_at']}")
        return

    # 사용량 조회 중 사용자가 `중지`/`리셋`으로 대기 작업을 취소했을 수 있다.
    if codex_deferred_jobs.get(channel_id) is not job:
        return

    consumed_prompts = list(job.get("queued_prompts") or [])
    consumed_images = list(job.get("queued_image_paths") or [])
    job["queued_prompts"] = []
    job["queued_image_paths"] = []
    job["resume_attempts"] = int(job.get("resume_attempts", 0)) + 1
    job["updated_at"] = _deferred_datetime_text(datetime.now())
    save_codex_deferred_jobs()

    channel_busy[channel_id] = True
    current_task = asyncio.current_task()
    if current_task:
        channel_tasks[channel_id] = current_task
    try:
        thread_id = job.get("thread_id")
        if thread_id:
            channel_codex_sessions[channel_id] = str(thread_id)
            save_codex_sessions()

        await channel.send(
            f"▶️ **Codex 사용량 복구 확인 · 자동 재개 시작** "
            f"(시도 {job['resume_attempts']}회)"
        )
        work_dir = str(job.get("work_dir") or channel_workdirs.get(channel_id, WORKING_DIR))
        resume_prompt = _build_deferred_resume_prompt(job, consumed_prompts)
        engine_prompt = add_screenshot_output_instruction(resume_prompt, work_dir)
        all_images = list(dict.fromkeys(list(job.get("image_paths") or []) + consumed_images))
        turn_started_at = time.time() - 1.0
        result = await run_codex(
            engine_prompt,
            work_dir,
            channel_id=channel_id,
            image_paths=all_images,
            status_channel=channel,
            reply_to=None,
        )
        if result.startswith("오류") or result == "(빈 응답)":
            raise RuntimeError(result)
        await send_screenshot_outputs_if_requested(
            channel, resume_prompt, work_dir, turn_started_at, result,
        )

        current = codex_deferred_jobs.get(channel_id)
        if current and (current.get("queued_prompts") or []):
            current["prompt"] = "\n\n".join(current.get("queued_prompts") or [])
            current["image_paths"] = list(current.get("queued_image_paths") or [])
            current["queued_prompts"] = []
            current["queued_image_paths"] = []
            current["thread_id"] = channel_codex_sessions.get(channel_id)
            current["retry_at"] = _deferred_datetime_text(datetime.now() + timedelta(seconds=5))
            current["resume_attempts"] = 0
            current["non_quota_failures"] = 0
            current["updated_at"] = _deferred_datetime_text(datetime.now())
            save_codex_deferred_jobs()
            await channel.send("📥 자동 재개 중 들어온 추가 요청도 이어서 처리할 예정입니다.")
        else:
            codex_deferred_jobs.pop(channel_id, None)
            save_codex_deferred_jobs()
            await channel.send("✅ **보관된 Codex 작업 자동 재개 완료**")
    except CodexUsageLimitError as e:
        current = codex_deferred_jobs.get(channel_id, job)
        current["queued_prompts"] = consumed_prompts + list(current.get("queued_prompts") or [])
        current["queued_image_paths"] = list(dict.fromkeys(
            consumed_images + list(current.get("queued_image_paths") or [])
        ))
        current["thread_id"] = e.thread_id or current.get("thread_id")
        current["retry_at"] = _deferred_datetime_text(e.retry_at)
        current["last_error"] = str(e)[-2000:]
        current["updated_at"] = _deferred_datetime_text(datetime.now())
        codex_deferred_jobs[channel_id] = current
        save_codex_deferred_jobs()
        await channel.send(f"⏸️ Codex 제한이 계속되어 `{current['retry_at']}`에 다시 시도합니다.")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        current = codex_deferred_jobs.get(channel_id, job)
        current["queued_prompts"] = consumed_prompts + list(current.get("queued_prompts") or [])
        current["queued_image_paths"] = list(dict.fromkeys(
            consumed_images + list(current.get("queued_image_paths") or [])
        ))
        current["non_quota_failures"] = int(current.get("non_quota_failures", 0)) + 1
        current["last_error"] = str(e)[-2000:]
        current["retry_at"] = _deferred_datetime_text(
            datetime.now() + timedelta(minutes=CODEX_DEFERRED_FALLBACK_RETRY_MINUTES)
        )
        current["updated_at"] = _deferred_datetime_text(datetime.now())
        codex_deferred_jobs[channel_id] = current
        save_codex_deferred_jobs()
        logger.error(f"채널 {channel_id}: Codex 자동 재개 실패: {e}", exc_info=True)
        await channel.send(
            f"⚠️ Codex 자동 재개 실패 ({current['non_quota_failures']}회). "
            f"`{current['retry_at']}`에 다시 시도합니다."
        )
        if current["non_quota_failures"] >= CODEX_CLAUDE_AUDIT_FAILURE_THRESHOLD:
            await _run_deferred_claude_audit(channel, channel_id, current, str(e))
    finally:
        channel_busy[channel_id] = False
        if channel_tasks.get(channel_id) is current_task:
            channel_tasks.pop(channel_id, None)
        channel_status.pop(channel_id, None)
        current = codex_deferred_jobs.get(channel_id)
        if current:
            current.pop("running", None)
            current.pop("started_at", None)
            save_codex_deferred_jobs()
        else:
            # 자동 재개 완료 메시지를 보내는 짧은 사이에 들어온 일반 요청도 놓치지 않는다.
            pending_list = _pop_channel_pending(channel_id)
            if pending_list:
                prompts = [item["prompt"] for item in pending_list]
                images = [path for item in pending_list for path in item.get("image_paths", [])]
                pending_message = pending_list[-1].get("message")
                if pending_message is None:
                    pending_message = await channel.send("▶ 재시작 전 대기 요청을 이어서 처리합니다.")
                channel_busy[channel_id] = True
                task = asyncio.create_task(_process_channel(
                    channel_id,
                    "\n\n".join(prompts),
                    pending_message,
                    images,
                ))
                channel_tasks[channel_id] = task


async def _process_channel(ch_id: int, prompt: str, message: discord.Message, image_paths: list[str]):
    """채널의 Claude 작업을 실행하고, 끝나면 대기 중인 메시지들을 합쳐서 이어서 처리"""
    current_task = asyncio.current_task()
    try:
        while True:
            quota_parked = False
            try:
                work_dir = channel_workdirs.get(ch_id, WORKING_DIR)
                # typing() 진입 실패(Discord 503 등)해도 턴 자체는 계속 진행
                typing_ctx = None
                try:
                    typing_ctx = message.channel.typing()
                    await typing_ctx.__aenter__()
                except Exception as e:
                    logger.warning(f"typing() 진입 실패, 인디케이터 없이 계속: {e}")
                    typing_ctx = None
                try:
                    backend = channel_backends.get(ch_id, DEFAULT_BACKEND)
                    engine = run_codex if backend == "codex" else run_claude
                    engine_prompt = add_screenshot_output_instruction(prompt, work_dir)
                    import time as _time
                    turn_started_at = _time.time() - 1.0
                    result = await engine(engine_prompt, work_dir, channel_id=ch_id,
                                          image_paths=image_paths,
                                          status_channel=message.channel,
                                          reply_to=message)
                finally:
                    if typing_ctx:
                        try:
                            await typing_ctx.__aexit__(None, None, None)
                        except Exception:
                            pass
                # 스트리밍으로 이미 전송됨. 에러/타임아웃 등 특수 경우만 별도 전송
                if result.startswith("⏰") or result.startswith("오류") or result == "(빈 응답)":
                    await message.reply(f"```\n{result}\n```")
                await send_screenshot_outputs_if_requested(message.channel, prompt, work_dir, turn_started_at, result)
            except asyncio.CancelledError:
                raise
            except CodexUsageLimitError as e:
                await _park_codex_job(
                    message.channel,
                    ch_id,
                    prompt,
                    work_dir,
                    image_paths,
                    e,
                    source="message",
                    source_message_id=getattr(message, "id", None),
                )
                quota_parked = True
            except Exception as e:
                logger.error(f"메시지 처리 중 오류: {e}", exc_info=True)
                try:
                    await message.reply(f"```\n오류 발생: {e}\n```")
                except Exception:
                    pass

            if quota_parked:
                pending_list = _pop_channel_pending(ch_id)
                for item in pending_list:
                    _append_deferred_request(
                        ch_id,
                        item["prompt"],
                        item.get("image_paths", []),
                    )
                if pending_list:
                    try:
                        await message.channel.send(
                            f"📥 작업 중 들어온 추가 요청 {len(pending_list)}개도 자동 재개 대기열에 보관했습니다."
                        )
                    except Exception:
                        pass
                break

            # 대기 중인 메시지들을 합쳐서 처리
            pending_list = _pop_channel_pending(ch_id)
            if pending_list:
                # 모든 대기 메시지의 프롬프트를 합치고, 마지막 메시지 객체로 응답
                all_prompts = []
                all_images = []
                for item in pending_list:
                    all_prompts.append(item["prompt"])
                    all_images.extend(item.get("image_paths", []))
                next_message = pending_list[-1].get("message")
                if next_message is None:
                    next_message = await message.channel.send("▶ 재시작 전 대기 요청을 이어서 처리합니다.")
                message = next_message  # 마지막 메시지에 답장
                prompt = "\n\n".join(all_prompts)
                image_paths = all_images
                logger.info(f"채널 {ch_id}: 대기 중이던 메시지 {len(pending_list)}개 합쳐서 처리")
            else:
                break
    finally:
        channel_busy[ch_id] = False
        if channel_tasks.get(ch_id) is current_task:
            channel_tasks.pop(ch_id, None)
        channel_status.pop(ch_id, None)


async def _run_scheduled_job(bot: commands.Bot, job_id: str, job: dict):
    ch_id = int(job.get("channel_id", 0))
    current_task = asyncio.current_task()
    channel = bot.get_channel(ch_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ch_id)
        except Exception as e:
            logger.warning(f"예약 작업 {job_id}: 채널 조회 실패 ({ch_id}): {e}")
            job["next_run"] = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            save_schedules()
            return

    if channel_busy.get(ch_id):
        mode = str(job.get("mode"))
        when = str(job.get("when"))
        try:
            if mode in ("daily", "every"):
                next_run = _next_run_for(mode, when)
            else:
                next_run = datetime.now() + timedelta(minutes=5)
            job["next_run"] = next_run.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            job["next_run"] = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        job.pop("running", None)
        job.pop("started_at", None)
        save_schedules()
        logger.info(f"예약 작업 {job_id}: 채널 busy로 연기 next={job.get('next_run')}")
        return

    prompt = str(job.get("prompt", "")).strip()
    if not prompt:
        scheduled_jobs.pop(job_id, None)
        save_schedules(deleted_ids={job_id})
        return

    channel_busy[ch_id] = True
    if current_task:
        channel_tasks[ch_id] = current_task
    try:
        await channel.send(f"⏰ 예약 작업 `{job_id}` 시작")
        work_dir = channel_workdirs.get(ch_id, WORKING_DIR)
        backend = channel_backends.get(ch_id, DEFAULT_BACKEND)
        engine = run_codex if backend == "codex" else run_claude
        engine_prompt = add_screenshot_output_instruction(prompt, work_dir)
        import time as _time
        turn_started_at = _time.time() - 1.0
        result = await engine(engine_prompt, work_dir, channel_id=ch_id,
                              image_paths=None, status_channel=channel, reply_to=None)
        if result.startswith("⏰") or result.startswith("오류") or result == "(빈 응답)":
            await channel.send(f"```\n{result}\n```")
        await send_screenshot_outputs_if_requested(channel, prompt, work_dir, turn_started_at, result)

        job["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if job.get("mode") == "once":
            scheduled_jobs.pop(job_id, None)
            save_schedules(deleted_ids={job_id})
        else:
            next_run = _next_run_for(str(job.get("mode")), str(job.get("when")))
            job["next_run"] = next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None
            job.pop("running", None)
            job.pop("started_at", None)
            save_schedules()
    except CodexUsageLimitError as e:
        await _park_codex_job(
            channel,
            ch_id,
            prompt,
            work_dir,
            None,
            e,
            source="scheduled",
            scheduled_job_id=job_id,
        )
        if job.get("mode") == "once":
            scheduled_jobs.pop(job_id, None)
            save_schedules(deleted_ids={job_id})
        else:
            next_run = _next_run_for(str(job.get("mode")), str(job.get("when")))
            job["next_run"] = next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None
            job.pop("running", None)
            job.pop("started_at", None)
            save_schedules()
    except asyncio.CancelledError:
        # 수동 중지는 이번 실행만 끝낸다. 반복 예약은 다음 정상 주기로 넘기고,
        # 1회 예약은 다시 즉시 실행되지 않도록 제거한다.
        if job.get("mode") == "once":
            scheduled_jobs.pop(job_id, None)
            save_schedules(deleted_ids={job_id})
        else:
            next_run = _next_run_for(str(job.get("mode")), str(job.get("when")))
            job["next_run"] = next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None
            job.pop("running", None)
            job.pop("started_at", None)
            save_schedules()
        logger.info(f"예약 작업 {job_id}: 사용자 중지로 현재 실행 취소")
        raise
    except Exception as e:
        logger.error(f"예약 작업 {job_id} 실행 실패: {e}", exc_info=True)
        try:
            await channel.send(f"```\n예약 작업 `{job_id}` 오류: {e}\n```")
        except Exception:
            pass
        job["next_run"] = (datetime.now() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        job.pop("running", None)
        job.pop("started_at", None)
        save_schedules()
    finally:
        channel_busy[ch_id] = False
        if channel_tasks.get(ch_id) is current_task:
            channel_tasks.pop(ch_id, None)
        channel_status.pop(ch_id, None)


async def _resume_persisted_channel_pending(bot: commands.Bot):
    """봇 재시작으로 실행 태스크가 사라진 일반 대기 요청을 채널별로 재개한다."""
    for channel_id, entries in list(channel_pending.items()):
        if not entries or channel_busy.get(channel_id) or channel_id in codex_deferred_jobs:
            continue
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception as e:
                logger.warning(f"채널 {channel_id}: 저장된 대기열 채널 조회 실패: {e}")
                continue

        restored = _pop_channel_pending(channel_id)
        if not restored:
            continue
        message = None
        message_id = restored[-1].get("message_id")
        if message_id and hasattr(channel, "fetch_message"):
            try:
                message = await channel.fetch_message(int(message_id))
            except Exception:
                message = None
        if message is None:
            try:
                message = await channel.send("▶ 봇 재시작 전 대기 요청을 이어서 처리합니다.")
            except Exception as e:
                logger.warning(f"채널 {channel_id}: 저장된 대기열 안내 실패: {e}")
                channel_pending[channel_id] = restored
                save_channel_pending()
                continue

        prompts = [entry["prompt"] for entry in restored]
        images = [
            path
            for entry in restored
            for path in entry.get("image_paths", [])
        ]
        channel_busy[channel_id] = True
        task = asyncio.create_task(_process_channel(
            channel_id,
            "\n\n".join(prompts),
            message,
            images,
        ))
        channel_tasks[channel_id] = task
        logger.info(f"채널 {channel_id}: 재시작 전 대기 요청 {len(restored)}개 복구")


async def _scheduler_loop(bot: commands.Bot):
    await bot.wait_until_ready()
    logger.info("예약 작업 + Codex 자동 재개 스케줄러 시작")
    while not bot.is_closed():
        try:
            now = datetime.now()
            due: list[tuple[str, dict]] = []
            for job_id, job in list(scheduled_jobs.items()):
                if job.get("running"):
                    continue
                next_s = job.get("next_run")
                if not next_s:
                    try:
                        next_run = _next_run_for(str(job.get("mode")), str(job.get("when")))
                        job["next_run"] = next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None
                        save_schedules()
                    except Exception as e:
                        logger.warning(f"예약 작업 {job_id}: next_run 계산 실패: {e}")
                        continue
                    next_s = job.get("next_run")
                try:
                    next_dt = datetime.strptime(str(next_s), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    logger.warning(f"예약 작업 {job_id}: 잘못된 next_run={next_s}")
                    continue
                if next_dt <= now:
                    due.append((job_id, job))

            for job_id, job in due:
                job["running"] = True
                job["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                save_schedules()
                asyncio.create_task(_run_scheduled_job(bot, job_id, job))

            for channel_id, job in list(codex_deferred_jobs.items()):
                if job.get("running"):
                    continue
                retry_at = _deferred_datetime(job.get("retry_at"), default=now)
                if retry_at > now:
                    continue
                job["running"] = True
                job["started_at"] = _deferred_datetime_text(now)
                save_codex_deferred_jobs()
                asyncio.create_task(_run_deferred_codex_job(bot, channel_id))
        except Exception as e:
            logger.error(f"예약 스케줄러 오류: {e}", exc_info=True)
        await asyncio.sleep(30)


async def _stop_channel_execution(channel_id: int) -> bool:
    """현재 실행과 자동 재개를 멈추되 Claude/Codex 대화 세션 ID는 유지한다."""
    had_work = _clear_channel_pending(channel_id)
    had_work = _cancel_deferred_codex_job(channel_id) or had_work

    task = channel_tasks.get(channel_id)
    if task and not task.done():
        task.cancel()
        had_work = True

    if await _stop_channel_codex_run(channel_id):
        had_work = True

    if channel_id in channel_claude_procs:
        await kill_channel_proc(channel_id)
        had_work = True

    if task and not task.done():
        try:
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True),
                timeout=10,
            )
        except asyncio.TimeoutError:
            logger.warning(f"채널 {channel_id}: 중지 태스크 정리 10초 초과")

    if channel_tasks.get(channel_id) is task:
        channel_tasks.pop(channel_id, None)
    channel_busy[channel_id] = False
    channel_status.pop(channel_id, None)
    return had_work


async def _handle_message(message: discord.Message, bot: commands.Bot):
    # Claude 카테고리 내 채널에서 허용된 유저의 메시지 처리
    if (
        is_claude_channel(message.channel)
        and message.author.id == ALLOWED_USER_ID
        and not message.content.startswith("!")
    ):
        prompt = message.content.strip()
        if not prompt:
            return

        ch_id = message.channel.id

        # 중지 명령 (태스크 cancel + 프로세스 종료, 세션 ID 유지)
        if prompt.casefold() in ("중지", "중지해줘", "stop", "멈춰", "멈춰줘", "취소", "esc"):
            had_work = await _stop_channel_execution(ch_id)
            if had_work:
                await message.reply("실행 중인 작업을 중지했습니다. 대화 맥락은 유지됩니다.")
            else:
                await message.reply("실행 중인 작업이 없습니다.")
            return

        # 리셋 명령 (태스크 cancel + 프로세스 종료 + 세션 ID 삭제)
        if prompt in ("리셋", "reset", "새대화"):
            await _stop_channel_execution(ch_id)
            channel_sessions.pop(ch_id, None)
            save_sessions()
            channel_codex_sessions.pop(ch_id, None)
            save_codex_sessions()
            await message.reply("세션 초기화 완료. 새 대화를 시작합니다.")
            return

        prompt, image_paths = await _prepare_prompt(message)

        backend = channel_backends.get(ch_id, DEFAULT_BACKEND)
        if backend == "codex" and ch_id in codex_deferred_jobs:
            job = _append_deferred_request(ch_id, prompt, image_paths)
            retry_at = job.get("retry_at") if job else "확인 중"
            queued_count = len(job.get("queued_prompts") or []) if job else 0
            await message.reply(
                f"📥 Codex 자동 재개 대기열에 추가했습니다. "
                f"(추가 요청 {queued_count}개, 재개 예정 `{retry_at}`)"
            )
            return

        # 현재 작업 중이면 조용히 대기 큐에 추가 (CLI처럼 자연스럽게)
        if channel_busy.get(ch_id):
            _queue_channel_pending(ch_id, prompt, message, image_paths)
            return

        # 작업 시작
        channel_busy[ch_id] = True
        task = asyncio.create_task(_process_channel(ch_id, prompt, message, image_paths))
        channel_tasks[ch_id] = task
        return


def _register_events(bot: commands.Bot):
    """봇 인스턴스에 이벤트와 명령어를 등록"""

    @bot.event
    async def on_ready():
        global scheduler_task
        print(f"봇 로그인 완료: {bot.user}")
        print(f"허용된 유저 ID: {ALLOWED_USER_ID}")
        print(f"작업 디렉토리: {WORKING_DIR}")
        # 슬래시 커맨드 동기화 (길드 단위면 즉시, 글로벌은 최대 1시간)
        try:
            synced = await bot.tree.sync()
            logger.info(f"슬래시 커맨드 {len(synced)}개 동기화 완료: {[c.name for c in synced]}")
            for guild in bot.guilds:
                guild_obj = discord.Object(id=guild.id)
                bot.tree.copy_global_to(guild=guild_obj)
                guild_synced = await bot.tree.sync(guild=guild_obj)
                logger.info(
                    f"길드 슬래시 커맨드 {len(guild_synced)}개 동기화 완료: "
                    f"guild={guild.id}, commands={[c.name for c in guild_synced]}"
                )
        except Exception as e:
            logger.error(f"슬래시 커맨드 sync 실패: {e}", exc_info=True)
        # 첨부파일 주기적 정리 시작
        asyncio.create_task(_attachment_cleanup_loop())
        if scheduler_task is None or scheduler_task.done():
            scheduler_task = asyncio.create_task(_scheduler_loop(bot))
        asyncio.create_task(_resume_persisted_channel_pending(bot))

        # 비정상 종료 후 재시작 감지 → 알림
        is_restart = os.path.exists(_RESTART_FLAG_FILE)
        # 플래그 파일 갱신 (정상 가동 중 표시)
        try:
            with open(_RESTART_FLAG_FILE, "w") as f:
                import time as _t
                f.write(str(_t.time()))
        except Exception:
            pass

        if is_restart and CLAUDE_CATEGORY_ID:
            try:
                category = bot.get_channel(CLAUDE_CATEGORY_ID)
                if category and category.text_channels:
                    ch = category.text_channels[0]
                    await ch.send("⚠️ **봇이 비정상 종료 후 자동 재시작되었습니다.**")
            except Exception as e:
                logger.error(f"재시작 알림 전송 실패: {e}")

    @bot.event
    async def on_disconnect():
        logger.warning("Discord 연결 끊김 - 자동 재연결 시도 중...")

    @bot.event
    async def on_error(event, *args, **kwargs):
        logger.error(f"이벤트 '{event}' 처리 중 예외 발생", exc_info=True)

    @bot.event
    async def on_message(message: discord.Message):
        """Claude 카테고리 내 채널에서는 모든 메시지를 Claude Code에 전달"""
        if message.author.bot:
            return

        try:
            await _handle_message(message, bot)
        except Exception as e:
            logger.error(f"on_message 처리 중 예외: {e}", exc_info=True)
            try:
                await message.reply(f"```\n내부 오류: {e}\n```")
            except Exception:
                pass

        try:
            await bot.process_commands(message)
        except Exception as e:
            logger.error(f"process_commands 처리 중 예외: {e}", exc_info=True)

    @bot.command(name="cc")
    async def claude_code(ctx: commands.Context, *, prompt: str):
        """!cc <프롬프트> - Claude Code 실행 (어느 채널에서든 사용 가능)"""
        if ctx.author.id != ALLOWED_USER_ID:
            await ctx.reply("권한이 없습니다.")
            return

        image_paths = []
        if ctx.message.attachments:
            image_paths = await download_attachments(ctx.message.attachments)

        if image_paths:
            images = [p for p in image_paths if os.path.splitext(p)[1].lower() in IMAGE_EXTS]
            texts = [p for p in image_paths if os.path.splitext(p)[1].lower() in TEXT_EXTS]
            parts = []
            if images:
                refs = "\n".join(f"- {p.replace(os.sep, '/')}" for p in images)
                parts.append(f"[첨부 이미지 - 반드시 Read 도구로 아래 경로를 읽어서 이미지를 확인한 뒤 답변해줘]:\n{refs}")
            if texts:
                refs = "\n".join(f"- {p.replace(os.sep, '/')}" for p in texts)
                parts.append(f"[첨부 파일 - 반드시 Read 도구로 아래 경로를 읽어서 내용을 확인한 뒤 답변해줘]:\n{refs}")
            prompt = f"{prompt}\n\n" + "\n\n".join(parts)

        work_dir = channel_workdirs.get(ctx.channel.id, WORKING_DIR)
        backend = channel_backends.get(ctx.channel.id, DEFAULT_BACKEND)
        if backend == "codex" and ctx.channel.id in codex_deferred_jobs:
            job = _append_deferred_request(ctx.channel.id, prompt, image_paths)
            await ctx.reply(
                f"📥 Codex 자동 재개 대기열에 추가했습니다. "
                f"(재개 예정 `{job.get('retry_at') if job else '확인 중'}`)"
            )
            return
        engine = run_codex if backend == "codex" else run_claude
        engine_prompt = add_screenshot_output_instruction(prompt, work_dir)
        try:
            async with ctx.typing():
                import time as _time
                turn_started_at = _time.time() - 1.0
                result = await engine(engine_prompt, work_dir, channel_id=ctx.channel.id,
                                      image_paths=image_paths,
                                      status_channel=ctx.channel,
                                      reply_to=ctx.message)
        except CodexUsageLimitError as e:
            await _park_codex_job(
                ctx.channel,
                ctx.channel.id,
                prompt,
                work_dir,
                image_paths,
                e,
                source="prefix",
                source_message_id=getattr(ctx.message, "id", None),
            )
            return

        if result.startswith("⏰") or result.startswith("오류") or result == "(빈 응답)":
            await ctx.reply(f"```\n{result}\n```")
        await send_screenshot_outputs_if_requested(ctx.channel, prompt, work_dir, turn_started_at, result)

    @bot.command(name="cd")
    async def change_dir(ctx: commands.Context, *, path: str):
        """!cd <경로> - 현재 채널의 작업 디렉토리 변경"""
        if ctx.author.id != ALLOWED_USER_ID:
            await ctx.reply("권한이 없습니다.")
            return

        resolved = os.path.abspath(path)
        if os.path.isdir(resolved):
            # 기존 프로세스 종료 (새 cwd로 재시작 필요)
            await kill_channel_proc(ctx.channel.id)
            channel_workdirs[ctx.channel.id] = resolved
            save_workdirs()
            ensure_claude_settings(resolved)
            await ctx.reply(f"이 채널의 작업 디렉토리 변경: `{resolved}`")
        else:
            await ctx.reply(f"존재하지 않는 경로: `{resolved}`")

    @bot.command(name="pwd")
    async def print_dir(ctx: commands.Context):
        """!pwd - 현재 채널의 작업 디렉토리 + 모델 + 백엔드 확인"""
        if ctx.author.id != ALLOWED_USER_ID:
            return
        work_dir = channel_workdirs.get(ctx.channel.id, WORKING_DIR)
        backend = channel_backends.get(ctx.channel.id, DEFAULT_BACKEND)
        model = _model_display(ctx.channel.id)
        reasoning = _reasoning_display(ctx.channel.id) if backend == "codex" else "(codex only)"
        await ctx.reply(
            f"📁 `{work_dir}`\n🤖 model: `{model}`\n"
            f"🧠 backend: `{backend}`\n💭 reasoning: `{reasoning}`"
        )

    @bot.command(name="backend")
    async def change_backend(ctx: commands.Context, *, name: str = ""):
        """!backend <claude|codex> - 채널 백엔드 변경. 인자 없으면 현재 확인."""
        if ctx.author.id != ALLOWED_USER_ID:
            return
        ch_id = ctx.channel.id
        name = name.strip().lower()
        if not name:
            current = channel_backends.get(ch_id, DEFAULT_BACKEND)
            await ctx.reply(f"현재 backend: `{current}` (사용 가능: claude / codex)")
            return
        if name not in BACKEND_OPTIONS:
            await ctx.reply(f"잘못된 backend: `{name}` (사용 가능: claude / codex)")
            return
        # claude로 돌아가면 codex 흔적은 없고, codex로 가면 기존 claude 프로세스 kill
        if name == "codex":
            await kill_channel_proc(ch_id)
        channel_backends[ch_id] = name
        save_backends()
        await ctx.reply(f"🧠 backend: `{name}` (다음 메시지부터 적용)")

    @bot.command(name="model")
    async def change_model(ctx: commands.Context, *, name: str = ""):
        """!model <opus|sonnet|haiku|클리어> - 채널 모델 변경. 인자 없으면 현재 모델 확인."""
        if ctx.author.id != ALLOWED_USER_ID:
            return
        ch_id = ctx.channel.id
        name = name.strip()
        if not name:
            current = channel_models.get(ch_id, "(default)")
            await ctx.reply(f"현재 모델: `{current}` (사용 가능: opus / sonnet / haiku / clear)")
            return
        if name.lower() in ("clear", "default", "기본", "해제"):
            channel_models.pop(ch_id, None)
            save_models()
            await kill_channel_proc(ch_id)
            await ctx.reply("모델 설정 해제. 기본값(default) 사용.")
            return
        channel_models[ch_id] = name
        save_models()
        await kill_channel_proc(ch_id)
        await ctx.reply(f"모델 변경: `{name}` (다음 메시지부터 적용)")

    @bot.command(name="select-model")
    async def select_model_cmd(ctx: commands.Context, *, name: str = ""):
        """!select-model <codex|claude-opus-4-8|...> - 백엔드와 모델을 함께 변경."""
        if ctx.author.id != ALLOWED_USER_ID:
            return
        if not name.strip():
            current_backend = channel_backends.get(ctx.channel.id, DEFAULT_BACKEND)
            current_model = _model_display(ctx.channel.id)
            available = "\n".join(
                f"- `{o['value']}`: {o['description']}"
                for o in _all_select_model_options()
            )
            await ctx.reply(
                f"현재 선택: `{current_backend}` / `{current_model}`\n"
                f"사용 가능:\n{available}"
            )
            return
        ok, message = await apply_selected_model(ctx.channel.id, name)
        await ctx.reply(message)

    @bot.command(name="reasoning")
    async def change_reasoning(ctx: commands.Context, *, level: str = ""):
        """!reasoning <low|medium|high|xhigh|max|ultra|default> - Codex 추론 강도 변경."""
        if ctx.author.id != ALLOWED_USER_ID:
            return
        ch_id = ctx.channel.id
        if not level.strip():
            model = _resolve_codex_model(ch_id)
            available = " / ".join(option["value"] for option in _reasoning_options(ch_id))
            await ctx.reply(
                f"현재 추론 강도: `{_reasoning_display(ch_id)}` / 모델: `{model}`\n"
                f"사용 가능: {available}"
            )
            return
        ok, message = apply_selected_reasoning(ch_id, level)
        await ctx.reply(message)

    @bot.command(name="doctor")
    async def codex_doctor_cmd(ctx: commands.Context):
        """!doctor — 현재 채널 작업 폴더 기준 Codex 설치/인증/런타임 진단."""
        if ctx.author.id != ALLOWED_USER_ID:
            return
        work_dir = channel_workdirs.get(ctx.channel.id, WORKING_DIR)
        status = await ctx.reply("🩺 Codex 진단 중...")
        _ok, report = await run_codex_doctor(work_dir)
        await status.edit(content=report[:1900])

    @bot.command(name="session")
    async def codex_session_cmd(ctx: commands.Context, action: str = "status"):
        """!session status|fork|archive — 채널 Codex 세션 관리."""
        if ctx.author.id != ALLOWED_USER_ID:
            return
        ch_id = ctx.channel.id
        work_dir = channel_workdirs.get(ch_id, WORKING_DIR)
        action = action.strip().lower()
        if action in ("status", "show", "상태"):
            session_id = channel_codex_sessions.get(ch_id)
            await ctx.reply(
                f"🧵 현재 Codex 세션: `{session_id}`"
                if session_id else "현재 채널에 연결된 Codex 세션이 없습니다."
            )
            return
        if action in ("fork", "분기"):
            _ok, result = await fork_codex_channel_session(ch_id, work_dir)
            await ctx.reply(result)
            return
        if action in ("archive", "보관"):
            _ok, result = await archive_codex_channel_session(ch_id, work_dir)
            await ctx.reply(result)
            return
        await ctx.reply("사용법: `!session status|fork|archive`")

    def _find_text_channel(ctx: commands.Context, name: str) -> discord.TextChannel | None:
        raw = name.strip()
        if not raw:
            return None
        if raw.startswith("<#") and raw.endswith(">"):
            raw = raw[2:-1]
        if raw.isdigit():
            ch = bot.get_channel(int(raw))
            return ch if isinstance(ch, discord.TextChannel) else None
        q = raw.lstrip("#").lower()
        candidates = ctx.guild.text_channels if ctx.guild else []
        for ch in candidates:
            if ch.name.lower() == q:
                return ch
        for ch in candidates:
            if q in ch.name.lower():
                return ch
        return None

    @bot.command(name="schedule")
    async def schedule_cmd(ctx: commands.Context, *, args: str = ""):
        """!schedule add/list/delete — 예약 작업 관리"""
        if ctx.author.id != ALLOWED_USER_ID:
            return
        try:
            parts = shlex.split(args)
        except ValueError as e:
            await ctx.reply(f"파싱 오류: {e}")
            return

        if not parts or parts[0].lower() in ("help", "?"):
            await ctx.reply(
                "사용법:\n"
                "`!schedule add daily 03:00 Youtube \"오늘 업로드 후보 분석하고 리포트 작성해\"`\n"
                "`!schedule add every 30m Normal \"프로세스 살아있는지 확인해\"`\n"
                "`!schedule add once \"2026-06-14 02:00\" Youtube \"새벽 테스트 빌드 돌려줘\"`\n"
                "`!schedule list`\n"
                "`!schedule delete 3`"
            )
            return

        action = parts[0].lower()
        if action == "list":
            refresh_schedules_from_disk()
            if not scheduled_jobs:
                await ctx.reply("등록된 예약 작업이 없습니다.")
                return
            lines = [
                _format_schedule(job_id, job)
                for job_id, job in sorted(
                    scheduled_jobs.items(),
                    key=lambda x: int(x[0]) if x[0].isdigit() else 999999,
                )
            ]
            for chunk in split_message("\n".join(lines), limit=1800):
                await ctx.reply(chunk)
            return

        if action in ("delete", "del", "remove", "rm"):
            if len(parts) < 2:
                await ctx.reply("사용법: `!schedule delete <id>`")
                return
            refresh_schedules_from_disk()
            job_id = parts[1].strip()
            removed = scheduled_jobs.pop(job_id, None)
            if not removed:
                await ctx.reply(f"예약 작업 `{job_id}`를 찾을 수 없습니다.")
                return
            save_schedules(deleted_ids={job_id})
            await ctx.reply(f"🗑️ 예약 삭제 `{job_id}`")
            return

        if action == "add":
            if len(parts) < 5:
                await ctx.reply(
                    "사용법: `!schedule add <daily|every|once> <when> <channel> \"prompt\"`\n"
                    "예: `!schedule add daily 03:00 Youtube \"오늘 업로드 후보 분석하고 리포트 작성해\"`"
                )
                return
            mode = parts[1].lower()
            if mode == "once" and len(parts) >= 6:
                when = f"{parts[2]} {parts[3]}"
                channel_name = parts[4]
                prompt = " ".join(parts[5:])
            else:
                when = parts[2]
                channel_name = parts[3]
                prompt = " ".join(parts[4:])
            target_channel = _find_text_channel(ctx, channel_name)
            if not target_channel:
                await ctx.reply(f"채널을 찾을 수 없습니다: `{channel_name}`")
                return
            try:
                job_id, job = _add_schedule_job(mode, when, prompt, target_channel.id)
            except ValueError as e:
                await ctx.reply(f"예약 형식 오류: {e}")
                return
            await ctx.reply(
                f"⏰ 예약 추가 `{job_id}`\n"
                f"채널: {target_channel.mention}\n"
                f"방식: `{job['mode']}` `{job['when']}`\n"
                f"다음 실행: `{job['next_run']}`"
            )
            return

        await ctx.reply("알 수 없는 schedule 명령입니다. `!schedule help`를 사용하세요.")

    @bot.command(name="skill")
    async def manage_skill(ctx: commands.Context, action: str = "list", *, name: str = ""):
        """!skill list|create|delete|show <이름> — 현재 백엔드의 스킬 관리."""
        if ctx.author.id != ALLOWED_USER_ID:
            await ctx.reply("권한이 없습니다.")
            return

        work_dir = channel_workdirs.get(ctx.channel.id, WORKING_DIR)
        backend = channel_backends.get(ctx.channel.id, DEFAULT_BACKEND)
        skills_dir = os.path.join(
            work_dir,
            ".agents" if backend == "codex" else ".claude",
            "skills",
        )

        if action == "list":
            all_skills = scan_all_skills(work_dir)
            if not all_skills:
                await ctx.reply("스킬이 없습니다.")
                return
            listing = "\n".join(
                f"• `{skill['name']}` · {skill['source']}"
                for skill in sorted(all_skills, key=lambda item: item["name"].casefold())
            )
            for i, chunk in enumerate(split_message(listing, limit=1750)):
                prefix = "**통합 스킬 목록:**\n" if i == 0 else ""
                await ctx.reply(prefix + chunk)

        elif action == "show":
            if not name:
                await ctx.reply("사용법: `!skill show <이름>`")
                return
            query = name.strip().casefold()
            match = next(
                (skill for skill in scan_all_skills(work_dir)
                 if skill["name"].casefold() == query and skill.get("path")),
                None,
            )
            skill_file = match.get("path") if match else None
            if not skill_file or not os.path.isfile(skill_file):
                await ctx.reply(f"스킬 `{name}`을(를) 찾을 수 없습니다.")
                return
            with open(skill_file, "r", encoding="utf-8") as f:
                content = f.read()
            for chunk in split_message(content):
                await ctx.reply(f"```markdown\n{chunk}\n```")

        elif action == "create":
            if not name:
                await ctx.reply("사용법: `!skill create <이름>` + .md 파일 첨부")
                return
            if not ctx.message.attachments:
                await ctx.reply(".md 파일을 첨부해주세요.")
                return
            att = ctx.message.attachments[0]
            if not att.filename.endswith(".md"):
                await ctx.reply(".md 파일만 가능합니다.")
                return
            content = (await att.read()).decode("utf-8")
            skill_path = os.path.join(skills_dir, name)
            os.makedirs(skill_path, exist_ok=True)
            with open(os.path.join(skill_path, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(content)
            await ctx.reply(f"{backend} 스킬 `{name}` 생성 완료: `{skill_path}`")

        elif action == "delete":
            if not name:
                await ctx.reply("사용법: `!skill delete <이름>`")
                return
            skill_path = os.path.join(skills_dir, name)
            if not os.path.isdir(skill_path):
                await ctx.reply(f"스킬 `{name}`을(를) 찾을 수 없습니다.")
                return
            import shutil
            shutil.rmtree(skill_path)
            await ctx.reply(f"스킬 `{name}` 삭제 완료.")

        else:
            await ctx.reply("사용법: `!skill list|create|delete|show <이름>`")

    # ---------- 슬래시 커맨드 ----------

    async def _run_prompt_from_interaction(interaction: discord.Interaction, prompt: str):
        """interaction에서 받은 프롬프트를 일반 메시지 처리 흐름에 끼워넣음"""
        await interaction.response.defer(thinking=True)
        sent_msg = await interaction.followup.send(f"▶ `{prompt[:80]}`")

        ch_id = interaction.channel.id
        backend = channel_backends.get(ch_id, DEFAULT_BACKEND)
        if backend == "codex" and ch_id in codex_deferred_jobs:
            job = _append_deferred_request(ch_id, prompt, [])
            await interaction.followup.send(
                f"📥 Codex 자동 재개 대기열에 추가했습니다. "
                f"(재개 예정 `{job.get('retry_at') if job else '확인 중'}`)"
            )
            return
        if channel_busy.get(ch_id):
            _queue_channel_pending(ch_id, prompt, sent_msg, [])
            return

        channel_busy[ch_id] = True
        task = asyncio.create_task(_process_channel(ch_id, prompt, sent_msg, []))
        channel_tasks[ch_id] = task

    @bot.tree.command(name="skill", description="스킬 실행 (자동완성 지원)")
    @app_commands.describe(name="실행할 스킬", args="추가 인자 (선택)")
    async def slash_skill(interaction: discord.Interaction, name: str, args: str = ""):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        prompt = _skill_invocation_prompt(interaction.channel.id, name, args)
        await _run_prompt_from_interaction(interaction, prompt)

    @slash_skill.autocomplete("name")
    async def _skill_ac(interaction: discord.Interaction, current: str):
        work_dir = channel_workdirs.get(interaction.channel.id, WORKING_DIR)
        return _skill_autocomplete_choices(work_dir, current)

    @bot.tree.command(name="cc", description="Claude Code 프롬프트 실행")
    @app_commands.describe(prompt="Claude에 전달할 프롬프트")
    async def slash_cc(interaction: discord.Interaction, prompt: str):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        await _run_prompt_from_interaction(interaction, prompt)

    @bot.tree.command(name="pwd", description="현재 채널 작업 디렉토리 + 모델 + 백엔드 확인")
    async def slash_pwd(interaction: discord.Interaction):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        work_dir = channel_workdirs.get(interaction.channel.id, WORKING_DIR)
        backend = channel_backends.get(interaction.channel.id, DEFAULT_BACKEND)
        model = _model_display(interaction.channel.id)
        reasoning = _reasoning_display(interaction.channel.id) if backend == "codex" else "(codex only)"
        await interaction.response.send_message(
            f"📁 `{work_dir}`\n🤖 model: `{model}`\n"
            f"🧠 backend: `{backend}`\n💭 reasoning: `{reasoning}`", ephemeral=True
        )

    @bot.tree.command(name="doctor", description="Codex 설치·인증·런타임 진단")
    async def slash_doctor(interaction: discord.Interaction):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        work_dir = channel_workdirs.get(interaction.channel.id, WORKING_DIR)
        _ok, report = await run_codex_doctor(work_dir)
        await interaction.followup.send(report[:1900], ephemeral=True)

    @bot.tree.command(name="session", description="현재 채널 Codex 세션 관리")
    @app_commands.describe(action="상태 확인, 분기 또는 보관")
    @app_commands.choices(action=[
        app_commands.Choice(name="상태 확인", value="status"),
        app_commands.Choice(name="새 세션으로 분기", value="fork"),
        app_commands.Choice(name="세션 보관", value="archive"),
    ])
    async def slash_session(interaction: discord.Interaction, action: str):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        ch_id = interaction.channel.id
        work_dir = channel_workdirs.get(ch_id, WORKING_DIR)
        if action == "status":
            session_id = channel_codex_sessions.get(ch_id)
            result = (
                f"🧵 현재 Codex 세션: `{session_id}`"
                if session_id else "현재 채널에 연결된 Codex 세션이 없습니다."
            )
            await interaction.response.send_message(result, ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        if action == "fork":
            _ok, result = await fork_codex_channel_session(ch_id, work_dir)
        else:
            _ok, result = await archive_codex_channel_session(ch_id, work_dir)
        await interaction.followup.send(result[:1900], ephemeral=True)

    @bot.tree.command(name="backend", description="채널 백엔드 변경 (claude / codex)")
    @app_commands.describe(name="claude 또는 codex")
    async def slash_backend(interaction: discord.Interaction, name: str):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        ch_id = interaction.channel.id
        name_l = name.strip().lower()
        if name_l not in BACKEND_OPTIONS:
            await interaction.response.send_message(
                f"잘못된 backend: `{name}` (사용 가능: claude / codex)", ephemeral=True
            )
            return
        if name_l == "codex":
            await kill_channel_proc(ch_id)
        channel_backends[ch_id] = name_l
        save_backends()
        await interaction.response.send_message(f"🧠 backend: `{name_l}` (다음 메시지부터 적용)")

    @slash_backend.autocomplete("name")
    async def _backend_ac(interaction: discord.Interaction, current: str):
        cur = current.lower().strip()
        matched = [b for b in BACKEND_OPTIONS if cur in b] if cur else BACKEND_OPTIONS
        return [app_commands.Choice(name=b, value=b) for b in matched]

    @bot.tree.command(name="model", description="채널 Claude 모델 변경 (opus/sonnet/haiku)")
    @app_commands.describe(name="모델 이름 (opus, sonnet, haiku, clear)")
    async def slash_model(interaction: discord.Interaction, name: str):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        ch_id = interaction.channel.id
        if name.lower() in ("clear", "default", "기본", "해제"):
            channel_models.pop(ch_id, None)
            save_models()
            await kill_channel_proc(ch_id)
            await interaction.response.send_message("모델 설정 해제. 기본값(default) 사용.")
            return
        channel_models[ch_id] = name
        save_models()
        await kill_channel_proc(ch_id)
        await interaction.response.send_message(f"🤖 모델 변경: `{name}` (다음 메시지부터 적용)")

    @slash_model.autocomplete("name")
    async def _model_ac(interaction: discord.Interaction, current: str):
        choices_src = MODEL_ALIASES + ["clear"]
        cur = current.lower().strip()
        matched = [m for m in choices_src if cur in m.lower()] if cur else choices_src
        return [app_commands.Choice(name=m, value=m) for m in matched[:25]]

    @bot.tree.command(name="select-model", description="백엔드와 모델을 함께 선택")
    @app_commands.describe(name="codex 또는 Claude 모델")
    async def slash_select_model(interaction: discord.Interaction, name: str):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        ok, message = await apply_selected_model(interaction.channel.id, name)
        await interaction.response.send_message(message, ephemeral=not ok)

    @slash_select_model.autocomplete("name")
    async def _select_model_ac(interaction: discord.Interaction, current: str):
        return _select_model_choices(current)

    @bot.tree.command(name="reasoning", description="채널 Codex 추론 강도 선택")
    @app_commands.describe(level="모델이 지원하는 추론 강도 또는 default")
    async def slash_reasoning(interaction: discord.Interaction, level: str):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        ok, message = apply_selected_reasoning(interaction.channel.id, level)
        await interaction.response.send_message(message, ephemeral=not ok)

    @slash_reasoning.autocomplete("level")
    async def _reasoning_ac(interaction: discord.Interaction, current: str):
        return _reasoning_choices(interaction.channel.id, current)

    @bot.tree.command(name="cd", description="채널 작업 디렉토리 변경")
    @app_commands.describe(path="변경할 디렉토리 경로")
    async def slash_cd(interaction: discord.Interaction, path: str):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        resolved = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(resolved):
            await interaction.response.send_message(f"존재하지 않는 경로: `{resolved}`", ephemeral=True)
            return
        await kill_channel_proc(interaction.channel.id)
        channel_workdirs[interaction.channel.id] = resolved
        save_workdirs()
        ensure_claude_settings(resolved)
        await interaction.response.send_message(f"작업 디렉토리 변경: `{resolved}`")

    @slash_cd.autocomplete("path")
    async def _cd_ac(interaction: discord.Interaction, current: str):
        try:
            expanded = os.path.expanduser(current) if current else os.path.expanduser("~")
            if os.path.isdir(expanded):
                base, prefix = expanded, ""
            else:
                base = os.path.dirname(expanded) or "."
                prefix = os.path.basename(expanded).lower()
            if not os.path.isdir(base):
                return []
            entries = []
            for e in os.listdir(base):
                full = os.path.join(base, e)
                if os.path.isdir(full) and e.lower().startswith(prefix):
                    entries.append(full)
            entries.sort()
            choices = []
            for e in entries[:25]:
                # value는 100자 제한 — 뒤에서 자르기 (전체 경로 유지 우선)
                value = e if len(e) <= 100 else e[-100:]
                choices.append(app_commands.Choice(name=value, value=value))
            return choices
        except Exception:
            return []

    @bot.tree.command(name="schedule_add", description="예약 작업 추가")
    @app_commands.describe(
        mode="daily / every / once",
        when="daily: 03:00, every: 30m/2h/1d, once: 2026-06-14 02:00",
        prompt="예약 시간에 실행할 프롬프트",
        channel="실행할 채널 (생략하면 현재 채널)",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="daily", value="daily"),
        app_commands.Choice(name="every", value="every"),
        app_commands.Choice(name="once", value="once"),
    ])
    async def slash_schedule_add(
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        when: str,
        prompt: str,
        channel: discord.TextChannel = None,
    ):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        mode_v = mode.value
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message("텍스트 채널에서만 예약할 수 있습니다.", ephemeral=True)
            return
        try:
            job_id, job = _add_schedule_job(mode_v, when, prompt, target_channel.id)
        except ValueError as e:
            await interaction.response.send_message(f"예약 형식 오류: {e}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"⏰ 예약 추가 `{job_id}`\n"
            f"채널: {target_channel.mention}\n"
            f"방식: `{job['mode']}` `{job['when']}`\n"
            f"다음 실행: `{job['next_run']}`"
        )

    @bot.tree.command(name="schedule_list", description="예약 작업 목록")
    async def slash_schedule_list(interaction: discord.Interaction):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        refresh_schedules_from_disk()
        if not scheduled_jobs:
            await interaction.response.send_message("등록된 예약 작업이 없습니다.", ephemeral=True)
            return
        lines = [_format_schedule(job_id, job) for job_id, job in sorted(scheduled_jobs.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999999)]
        text = "\n".join(lines)
        chunks = split_message(text, limit=1800)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @bot.tree.command(name="schedule_delete", description="예약 작업 삭제")
    @app_commands.describe(job_id="삭제할 예약 작업 ID")
    async def slash_schedule_delete(interaction: discord.Interaction, job_id: str):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        refresh_schedules_from_disk()
        job_id = job_id.strip()
        removed = scheduled_jobs.pop(job_id, None)
        if not removed:
            await interaction.response.send_message(f"예약 작업 `{job_id}`를 찾을 수 없습니다.", ephemeral=True)
            return
        save_schedules(deleted_ids={job_id})
        await interaction.response.send_message(f"🗑️ 예약 삭제 `{job_id}`")

    @slash_schedule_delete.autocomplete("job_id")
    async def _schedule_delete_ac(interaction: discord.Interaction, current: str):
        refresh_schedules_from_disk()
        cur = current.strip()
        choices = []
        for job_id, job in sorted(scheduled_jobs.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999999):
            if cur and cur not in job_id:
                continue
            label = _format_schedule(job_id, job)
            choices.append(app_commands.Choice(name=label[:100], value=job_id))
            if len(choices) >= 25:
                break
        return choices

    async def _send_files_as_attachments(interaction: discord.Interaction, paths: list[str]):
        """파일 경로 리스트를 디스코드 첨부로 전송. 크기/개수 제한 적용."""
        limit_bytes = getattr(interaction.guild, "filesize_limit", 25 * 1024 * 1024) or (25 * 1024 * 1024)
        skipped: list[str] = []
        too_big: list[str] = []
        valid: list[str] = []
        for p in paths:
            if not os.path.isfile(p):
                skipped.append(f"`{p}` (없음)")
                continue
            sz = os.path.getsize(p)
            if sz == 0:
                skipped.append(f"`{os.path.basename(p)}` (빈 파일)")
                continue
            if sz > limit_bytes:
                too_big.append(f"`{os.path.basename(p)}` ({sz/1024/1024:.1f}MB)")
                continue
            valid.append(p)

        if not valid and not too_big and not skipped:
            await interaction.followup.send("📭 보낼 파일이 없습니다.")
            return

        # Discord는 한 메시지에 최대 10개 첨부 — 청크로 나눠 보냄
        for i in range(0, len(valid), 10):
            chunk = valid[i:i+10]
            files = [discord.File(p, filename=os.path.basename(p)) for p in chunk]
            names = ", ".join(f"`{os.path.basename(p)}`" for p in chunk)
            try:
                await interaction.followup.send(f"📎 {names}", files=files)
            except Exception as e:
                await interaction.followup.send(f"전송 실패 ({len(chunk)}개): {e}")

        notes = []
        if too_big:
            notes.append(f"⚠️ 크기 초과(전송 불가): {', '.join(too_big)}")
        if skipped:
            notes.append(f"⚠️ 건너뜀: {', '.join(skipped)}")
        if notes:
            await interaction.followup.send("\n".join(notes))

    @bot.tree.command(name="files", description="직전 턴에서 Claude가 작업한 파일들 첨부로 받기")
    async def slash_files(interaction: discord.Interaction):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        files = channel_recent_files.get(interaction.channel.id, [])
        if not files:
            await interaction.response.send_message("직전 턴에서 Claude가 작업한 파일이 없습니다.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await _send_files_as_attachments(interaction, files)

    @bot.tree.command(name="file", description="특정 파일 다운로드 (작업 디렉토리 기준)")
    @app_commands.describe(path="파일 경로 (작업 디렉토리 상대경로 또는 절대경로)")
    async def slash_file(interaction: discord.Interaction, path: str):
        if interaction.user.id != ALLOWED_USER_ID:
            await interaction.response.send_message("권한이 없습니다.", ephemeral=True)
            return
        work_dir = channel_workdirs.get(interaction.channel.id, WORKING_DIR)
        resolved = path if os.path.isabs(path) else os.path.join(work_dir, path)
        resolved = os.path.normpath(resolved)
        await interaction.response.defer(thinking=True)
        await _send_files_as_attachments(interaction, [resolved])

    @slash_file.autocomplete("path")
    async def _file_ac(interaction: discord.Interaction, current: str):
        try:
            work_dir = channel_workdirs.get(interaction.channel.id, WORKING_DIR)
            # 우선순위 1: 직전 턴 작업 파일 매칭
            recent = channel_recent_files.get(interaction.channel.id, [])
            cur = current.lower().strip()
            choices: list[app_commands.Choice] = []
            seen: set[str] = set()
            for abs_p in recent:
                try:
                    rel = os.path.relpath(abs_p, work_dir)
                except ValueError:
                    rel = abs_p
                disp = rel.replace("\\", "/")
                if cur and cur not in disp.lower():
                    continue
                if disp in seen:
                    continue
                seen.add(disp)
                value = disp if len(disp) <= 100 else disp[-100:]
                choices.append(app_commands.Choice(name=f"🆕 {value}"[:100], value=value))

            # 우선순위 2: 작업 디렉토리 prefix 검색
            if len(choices) < 25 and current:
                expanded = current
                target_abs = expanded if os.path.isabs(expanded) else os.path.join(work_dir, expanded)
                if os.path.isdir(target_abs):
                    base, prefix = target_abs, ""
                else:
                    base = os.path.dirname(target_abs) or work_dir
                    prefix = os.path.basename(target_abs).lower()
                if os.path.isdir(base):
                    for e in sorted(os.listdir(base)):
                        full = os.path.join(base, e)
                        if not os.path.isfile(full):
                            continue
                        if prefix and not e.lower().startswith(prefix):
                            continue
                        try:
                            rel = os.path.relpath(full, work_dir).replace("\\", "/")
                        except ValueError:
                            rel = full
                        if rel in seen:
                            continue
                        seen.add(rel)
                        value = rel if len(rel) <= 100 else rel[-100:]
                        choices.append(app_commands.Choice(name=value, value=value))
                        if len(choices) >= 25:
                            break
            return choices[:25]
        except Exception:
            return []


if __name__ == "__main__":
    # 단일 인스턴스 보장: 이미 실행 중이면 즉시 종료
    import socket as _sock
    _lock_socket = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    try:
        _lock_socket.bind(("127.0.0.1", 39517))
    except _sock.error:
        print("이미 실행 중인 인스턴스가 있습니다. 종료합니다.")
        exit(1)

    if not TOKEN:
        print("DISCORD_TOKEN이 설정되지 않았습니다. .env 파일을 확인하세요.")
        exit(1)
    if ALLOWED_USER_ID == 0:
        print("ALLOWED_USER_ID가 설정되지 않았습니다. .env 파일을 확인하세요.")
        exit(1)

    try:
        bot = create_bot()
        logger.info("봇 시작...")
        bot.run(TOKEN, reconnect=True)
    except Exception as e:
        logger.error(f"봇이 예외로 종료됨: {e}", exc_info=True)
    logger.warning("봇이 종료됨.")
