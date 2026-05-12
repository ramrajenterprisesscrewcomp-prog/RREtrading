"""
tsr_service.py
Fetches TSR Pro data by calling tsr_worker.py as a subprocess.
The worker runs its own Python process + ProactorEventLoop — no event-loop
conflicts with uvicorn's SelectorEventLoop.
"""
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_WORKER    = Path(__file__).parent.parent / "tsr_worker.py"
_PYTHON    = sys.executable
_CACHE_TTL  = 300   # seconds between refreshes
_RETRY_WAIT = 60    # seconds to wait after a failed fetch
_TIMEOUT    = 180   # max seconds for one scrape run

# Shared state — written by background thread, read by FastAPI handlers
_tsr_data:   dict  = {}
_last_fetch: float = 0.0
_lock = threading.Lock()
_bg_started = False


def _run_worker(email: str, password: str) -> dict:
    """Call tsr_worker.py as a subprocess. Returns parsed JSON dict or {}."""
    try:
        proc = subprocess.run(
            [_PYTHON, str(_WORKER), email, password],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            cwd=str(_WORKER.parent),
        )
        logger.info("TSR worker exit=%d stdout_len=%d", proc.returncode, len(proc.stdout))
        if proc.stderr:
            logger.warning("TSR worker stderr: %s", proc.stderr[:2000])
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        # stdout may have Playwright/Python warnings before the JSON line
        for line in reversed(proc.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    return data if isinstance(data, dict) else {}
                except json.JSONDecodeError:
                    pass
        logger.warning("TSR: no JSON found in worker stdout: %s", proc.stdout[:500])
        return {}
    except subprocess.TimeoutExpired:
        logger.warning("TSR: worker timed out after %ds", _TIMEOUT)
        return {}
    except Exception as exc:
        logger.warning("TSR: worker call failed: %s", exc)
        return {}


def _background_loop(email: str, password: str) -> None:
    global _tsr_data, _last_fetch
    logger.info("TSR: background fetcher started")
    while True:
        logger.info("TSR: running worker subprocess…")
        data = _run_worker(email, password)
        if data:
            with _lock:
                _tsr_data   = data
                _last_fetch = time.time()
            total = (
                len(data.get("buildup", {}).get("long_buildup", [])) +
                len(data.get("buildup", {}).get("short_buildup", [])) +
                len(data.get("pcr", [])) +
                len(data.get("signals", []))
            )
            logger.info("TSR: cache refreshed (%d total rows) — sleeping %ds",
                        total, _CACHE_TTL)
            time.sleep(_CACHE_TTL)
        else:
            logger.warning("TSR: worker returned empty — retrying in %ds", _RETRY_WAIT)
            time.sleep(_RETRY_WAIT)


def start_background_fetcher() -> None:
    """Start daemon thread. Safe to call multiple times."""
    global _bg_started
    if _bg_started:
        return
    from config import TSR_EMAIL, TSR_PASSWORD
    if not TSR_EMAIL or not TSR_PASSWORD:
        logger.info("TSR: credentials not configured, skipping")
        return
    if not _WORKER.exists():
        logger.warning("TSR: tsr_worker.py not found at %s", _WORKER)
        return
    _bg_started = True
    threading.Thread(
        target=_background_loop,
        args=(TSR_EMAIL, TSR_PASSWORD),
        daemon=True,
        name="tsr-fetcher",
    ).start()
    logger.info("TSR: background fetcher thread launched")


# ── Public API (non-blocking reads from shared cache) ─────────────────────

async def get_tsr_all() -> dict:
    with _lock:
        return dict(_tsr_data)

async def get_tsr_buildup() -> dict:
    return (await get_tsr_all()).get("buildup", {})

async def get_tsr_pcr() -> list:
    return (await get_tsr_all()).get("pcr", [])

async def get_tsr_signals() -> list:
    return (await get_tsr_all()).get("signals", [])

async def get_tsr_support() -> dict:
    d = await get_tsr_all()
    return {
        "weekly":  d.get("weekly_support", []),
        "monthly": d.get("monthly_support", []),
    }
