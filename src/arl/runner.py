"""Run a job with limits, stop it cleanly, and write down the conditions it ran under.

A job runs in a session of its own, so that everything it starts can be found
and stopped together, whatever process groups it makes. A job is stopped by
asking first (SIGTERM, so it can save its state) and forcing after a grace
period.
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import glob
import json
import math
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Generator

from .config import Config
from .ledger import Ledger
from .util import EXIT_BUSY, ArlError, canonical, read_json, read_text, sha256_file, sha256_text, write_json

_PLACEHOLDER = re.compile(r"\{(params|out|metrics|split|home|research|exp|worktree)\}")
QUICK_GRACE = 10.0  # seconds a job gets to end when it is stopped for a limit


def substitute(template: str, values: dict[str, Any]) -> str:
    """Fill {name} placeholders, quoted for the shell. Other braces are left alone."""
    return _PLACEHOLDER.sub(lambda m: shlex.quote(str(values[m.group(1)])), template)


# --- is the machine free? ---------------------------------------------------------


def busy(cfg: Config) -> list[str]:
    """Reasons not to start a run now. Empty means go."""
    reasons = []
    if cfg.max_loadavg is not None:
        load = os.getloadavg()[0]
        if load > cfg.max_loadavg:
            reasons.append(f"load average {load:.1f} is above {cfg.max_loadavg}")
    if cfg.min_free_ram_gb is not None:
        free = _mem_available_gb()
        if free is not None and free < cfg.min_free_ram_gb:
            reasons.append(f"{free:.1f} GB of RAM available, {cfg.min_free_ram_gb} wanted")
    if cfg.min_free_gpu_mb is not None:
        free_gpu = _gpu_free_mb()
        if free_gpu is None:
            reasons.append("no GPU answered nvidia-smi")
        elif free_gpu < cfg.min_free_gpu_mb:
            reasons.append(f"{free_gpu:.0f} MB of GPU memory free, {cfg.min_free_gpu_mb} wanted")
    return reasons


def _mem_available_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024**2
    except OSError:
        pass
    return None


def _nvidia(query: str) -> list[str] | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip().splitlines() if out.returncode == 0 and out.stdout.strip() else None


def _gpu_free_mb() -> float | None:
    rows = _nvidia("memory.total,memory.used")
    if not rows:
        return None
    try:
        return max(float(t) - float(u) for t, u in (row.split(",") for row in rows))
    except ValueError:
        return None


@contextlib.contextmanager
def run_lock(ledger: Ledger) -> Generator[None, None, None]:
    """One run at a time, across every project that names the same lock file."""
    cfg = ledger.cfg
    path = Path(os.path.expanduser(cfg.lock)) if cfg.lock else ledger.dir / ".run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            f.seek(0)
            raise ArlError(f"another run holds {path} ({f.read().strip() or 'unknown'})", EXIT_BUSY) from e
        f.seek(0)
        f.truncate()
        f.write(f"pid {os.getpid()} {ledger.cfg.name}")
        f.flush()
        try:
            yield
        finally:
            f.seek(0)
            f.truncate()


# --- processes ----------------------------------------------------------------------


def _stat_fields(pid: int | str) -> list[str] | None:
    """/proc/<pid>/stat after the command name: state, ppid, pgrp, session, ..."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2 :].split()
    except (OSError, ValueError):
        return None


def boot_id() -> str:
    """Changes with every start of the machine: pids from before it mean nothing."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def proc_start(pid: int | None) -> int | None:
    """When a process started, in clock ticks since boot. Tells a live pid from a reused one."""
    fields = _stat_fields(pid) if pid else None
    try:
        return int(fields[19]) if fields else None
    except (ValueError, IndexError):
        return None


def alive(pid: int | None, start: int | None = None) -> bool:
    """The process exists and, when its start time is known, is the same one."""
    now_start = proc_start(pid)
    if now_start is None:
        return False
    return start is None or now_start == start


def session_pids(sid: int) -> list[int]:
    """Every process of the session a job started, whatever its process group."""
    try:
        entries = [e.name for e in os.scandir("/proc") if e.name.isdigit()]
    except OSError:
        return []
    out = []
    for pid in entries:
        fields = _stat_fields(pid)
        try:
            if fields and int(fields[3]) == sid and fields[0] != "Z":
                out.append(int(pid))
        except (ValueError, IndexError):
            continue
    return out


def _memory_kb(pid: int) -> int:
    """Memory a process holds, shared pages counted once across those that share them."""
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    fields = _stat_fields(pid)  # no smaps_rollup: fall back to the resident size
    try:
        return int(fields[21]) * os.sysconf("SC_PAGE_SIZE") // 1024 if fields else 0
    except (ValueError, IndexError):
        return 0


def _session_memory_gb(sid: int) -> float:
    return sum(_memory_kb(pid) for pid in session_pids(sid)) / 1024**2


def child_ref(pid: int) -> dict[str, Any]:
    """What it takes to find a job again later and be sure it is the same one."""
    return {"sid": pid, "start": proc_start(pid), "boot": boot_id()}


def session_of(ref: dict[str, Any] | None) -> int | None:
    """The session id of a recorded job if it can still be the same job, else None."""
    if not ref or ref.get("boot") != boot_id():
        return None  # the machine restarted: the job died with it
    sid = ref.get("sid")
    start = proc_start(sid)
    if start is not None and start != ref.get("start"):
        return None  # the number was given to another process, so the old session is gone
    return sid


def stop_session(sid: int, grace: float = QUICK_GRACE) -> bool:
    """Stop every process of a session: ask, wait, then force. True if anything was alive.

    Signals that would interrupt arl itself are held off while this runs, so a
    second Ctrl-C or a hangup cannot leave the job half stopped.
    """
    held = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            held[sig] = signal.signal(sig, signal.SIG_IGN)
    except ValueError:
        pass  # not the main thread
    try:
        found = False
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, QUICK_GRACE)):
            pids = session_pids(sid)
            if not pids:
                return found
            found = True
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            end = time.monotonic() + wait
            while time.monotonic() < end and session_pids(sid):
                time.sleep(0.1)
        return found
    finally:
        for sig, handler in held.items():
            signal.signal(sig, handler)


# --- running ----------------------------------------------------------------------


def execute(
    command: str,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    log: Path,
    max_rss_gb: float | None = None,
    min_free_ram_gb: float | None = None,
    on_start: Callable[[int], None] | None = None,
    pause_file: Path | None = None,
    pause_grace: float = QUICK_GRACE,
) -> dict[str, Any]:
    """Run through bash in a session of its own, output to a file.

    Returns exit_code, seconds and two texts of which at most one is set:
    "stopped" when a limit ended the job (its time, its memory), and "paused"
    when it was asked to save and stop (a pause request, the machine running
    low on memory, or arl itself being interrupted). A paused job can be
    continued later; a stopped one failed.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    stopped = paused = ""
    with open(log, "ab") as f:
        f.write(f"$ cd {cwd} && {command}\n".encode())
        f.flush()
        proc = subprocess.Popen(
            ["bash", "-c", command],
            cwd=cwd,
            env=env,
            stdout=f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            if on_start:
                on_start(proc.pid)  # the session id of everything the job starts
            ticks = 0
            while True:
                try:
                    proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    pass
                ticks += 1
                if time.monotonic() - start > timeout:
                    stopped = f"it passed its limit of {timeout:.0f} s"
                elif pause_file is not None and pause_file.exists():
                    paused = "a pause was asked for"
                elif ticks % 2 == 0 and max_rss_gb is not None and (held := _session_memory_gb(proc.pid)) > max_rss_gb:
                    stopped = f"its processes held {held:.1f} GB of RAM, the limit is {max_rss_gb}"
                elif min_free_ram_gb is not None and (free := _mem_available_gb()) is not None and free < min_free_ram_gb:
                    paused = f"the machine was down to {free:.1f} GB of free RAM, the floor is {min_free_ram_gb}"
                if stopped or paused:
                    stop_session(proc.pid, pause_grace if paused else QUICK_GRACE)
                    proc.wait()
                    break
        except BaseException:
            # arl itself is being stopped: give the job the time to save, and take it down.
            stop_session(proc.pid, pause_grace)
            proc.wait()
            raise
        finally:
            if pause_file is not None:
                pause_file.unlink(missing_ok=True)
        if not (stopped or paused) and session_pids(proc.pid):
            stop_session(proc.pid)  # the job ended and left something running: nothing outlives a run
    return {"exit_code": proc.returncode, "seconds": round(time.monotonic() - start, 2), "stopped": stopped, "paused": paused}


def last_checkpoint(out: Path) -> float | None:
    """When the job last wrote into {out}/checkpoint, as a time stamp."""
    folder = out / "checkpoint"
    if not folder.is_dir():
        return None
    times = [p.stat().st_mtime for p in folder.rglob("*") if p.is_file()]
    return max(times) if times else None


def tail(path: Path, lines: int = 120, max_bytes: int = 48_000) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        text = f.read().decode(errors="replace")
    return "\n".join(text.splitlines()[-lines:]) + "\n"


# --- conditions -------------------------------------------------------------------


def environment(ledger: Ledger) -> dict[str, Any]:
    """Where and with what the run happens. The package list is stored once per content."""
    info: dict[str, Any] = {
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "arl_python": sys.version.split()[0],
    }
    gpu = _nvidia("name,driver_version")
    if gpu:
        info["gpu"] = [row.strip() for row in gpu]
    cmd = ledger.cfg.env_command
    if cmd:
        out = subprocess.run(["bash", "-c", cmd], cwd=ledger.home, capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            raise ArlError(f"[env] command failed: {cmd}\n{out.stderr.strip()[-2000:]}")
        digest = sha256_text(out.stdout)[:16]
        ledger.env_root.mkdir(parents=True, exist_ok=True)
        listing = ledger.env_root / f"{digest}.txt"
        if not listing.exists():
            listing.write_text(out.stdout)
        info["packages"] = digest
    return info


def data_fingerprint(ledger: Ledger) -> list[dict[str, Any]]:
    """Size and sha256 of each [data] file. Hashes are cached by size and mtime."""
    patterns = ledger.cfg.data_files
    if not patterns:
        return []
    cache_path = ledger.cache_root / "hashes.json"
    try:
        cache = read_json(cache_path) if cache_path.exists() else {}
    except (ArlError, json.JSONDecodeError):
        cache = {}
    out = []
    for pattern in patterns:
        expanded = os.path.expanduser(pattern)
        if not os.path.isabs(expanded):
            expanded = str(ledger.home / expanded)
        matches = sorted(glob.glob(expanded, recursive=True))
        if not matches:
            raise ArlError(f"[data] files: nothing matches '{pattern}'")
        for name in matches:
            path = Path(name)
            if not path.is_file():
                continue
            stat = path.stat()
            key = f"{path}|{stat.st_size}|{stat.st_mtime_ns}"
            if key not in cache:
                cache[key] = sha256_file(path)
            out.append({"path": str(path), "bytes": stat.st_size, "sha256": cache[key]})
    ledger.cache_root.mkdir(parents=True, exist_ok=True)
    write_json(cache_path, cache)
    return out


def data_digest(fingerprint: list[dict[str, Any]]) -> str:
    """One hash for the data a run read. Results are compared only on the same data."""
    return sha256_text(canonical([(d["path"], d["sha256"]) for d in fingerprint]))


def manifest(out_dir: Path, hash_up_to: int = 1 << 31) -> list[dict[str, Any]]:
    """What the run left behind. Artifacts stay out of git, their hashes go in."""
    items = []
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file():
            continue
        size = path.stat().st_size
        item: dict[str, Any] = {"path": path.relative_to(out_dir).as_posix(), "bytes": size}
        if size <= hash_up_to:
            item["sha256"] = sha256_file(path)
        items.append(item)
    return items


def keep_small(out_dir: Path, kept_dir: Path, patterns: list[str], max_kb: int) -> list[str]:
    """Copy small result files into the ledger so they are committed."""
    kept = []
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file() or path.stat().st_size > max_kb * 1024:
            continue
        rel = path.relative_to(out_dir).as_posix()
        if rel.startswith("checkpoint/"):
            continue  # saved state is for continuing a run, not a result
        if any(fnmatch.fnmatch(path.name, p) or fnmatch.fnmatch(rel, p) for p in patterns):
            target = kept_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            kept.append(rel)
    return kept


def read_metrics(path: Path, cfg: Config) -> dict[str, float]:
    """The evaluator's numbers. A missing or non-finite metric is a failed evaluation.

    Other numbers the evaluator reports are kept when they are finite.
    """
    if not path.exists():
        raise ArlError(f"the evaluator wrote no metrics file ({path})")
    try:
        data = json.loads(read_text(path))
    except json.JSONDecodeError as e:
        raise ArlError(f"the evaluator's metrics file is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise ArlError("the evaluator's metrics file must hold one JSON object")
    numbers = {k: v for k, v in data.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    for m in cfg.metrics:
        if m.name not in numbers:
            raise ArlError(f"the evaluator did not report '{m.name}'")
        if not math.isfinite(numbers[m.name]):
            raise ArlError(f"the evaluator reported {m.name} = {numbers[m.name]}")
    return {k: float(v) for k, v in numbers.items() if math.isfinite(v)}
