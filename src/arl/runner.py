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

from . import __version__
from .config import Config
from .gitops import Git
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


# --- the environment a job gets -----------------------------------------------------

_PROBE = """\
import json, sys
print(json.dumps({"prefix": sys.prefix, "python": sys.version.split()[0]}))
"""
_LISTING = """\
import importlib.metadata as m, json, os, subprocess, sys

def git(path, *args):
    out = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=30)
    return out.stdout.strip() if out.returncode == 0 else None

lines = set()
for d in m.distributions():
    name = d.metadata["Name"] if d.metadata else None
    if not name:
        continue
    line = name + "==" + d.version
    try:
        url = json.loads(d.read_text("direct_url.json") or "{}")
    except Exception:
        url = {}
    where = str(url.get("url", ""))
    if url.get("dir_info", {}).get("editable") and where.startswith("file://"):
        # An editable install is whatever its folder holds now: say which commit, and whether it was changed.
        path = where[7:]
        line += " (editable " + path
        try:
            head = git(path, "rev-parse", "HEAD")
            if head:
                # --no-optional-locks: only looking, in a folder somebody else may be working in
                line += " at " + head[:12] + (", modified" if git(path, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no") else "")
        except Exception:
            pass
        line += ")"
    lines.add(line)
print("python==" + sys.version.split()[0])
print("\\n".join(sorted(lines, key=str.lower)))
meta = os.path.join(sys.prefix, "conda-meta")
if os.path.isdir(meta):  # what conda installed, with the build: also the libraries python does not list
    for f in sorted(os.listdir(meta), key=str.lower):
        if f.endswith(".json") and f.count("-") >= 2:
            name, version, build = f[:-5].rsplit("-", 2)
            print("conda: " + name + "=" + version + "=" + build)
"""
_POINTS_ELSEWHERE = ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV")


def env_prefix(ledger: Ledger) -> Path | None:
    """The environment the contract names ([env] prefix), as a folder on this machine."""
    text = ledger.cfg.env_prefix
    if not text:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    return path if path.is_absolute() else ledger.home / path


def job_env(ledger: Ledger) -> dict[str, str]:
    """What every job starts with: the caller's environment, with the one the contract
    names in front and without what would point python somewhere else. So a run is the
    same whether an agent's session or a person's terminal started it."""
    env = dict(os.environ)
    prefix = env_prefix(ledger)
    if prefix is None:
        return env
    for name in _POINTS_ELSEWHERE:
        env.pop(name, None)
    env["PATH"] = f"{prefix / 'bin'}{os.pathsep}{env.get('PATH', os.defpath)}"
    env["PYTHONNOUSERSITE"] = "1"  # packages installed with --user are not part of the environment
    if (prefix / "conda-meta").is_dir():
        env["CONDA_PREFIX"], env["CONDA_DEFAULT_ENV"] = str(prefix), prefix.name
    elif (prefix / "pyvenv.cfg").is_file():
        env["VIRTUAL_ENV"] = str(prefix)
    return env


def probe_env(ledger: Ledger) -> dict[str, Any] | None:
    """Ask the python a job would get what it is: its prefix, as the contract writes
    it, and its version. None when the contract names no environment. Raises when
    that python is not the environment the contract names."""
    cfg = ledger.cfg
    prefix = env_prefix(ledger)
    if prefix is None:
        return None
    if not (prefix / "bin" / "python").exists():
        raise ArlError(f"The contract names the environment {cfg.env_prefix} ([env] prefix), and it is not on this machine: no {prefix / 'bin' / 'python'}.")
    detail = ""
    try:
        out = subprocess.run(["bash", "-c", 'exec python -I -c "$0"', _PROBE], cwd=ledger.home, env=job_env(ledger), capture_output=True, text=True, timeout=120)
        # The last line: something a start-up file of the environment prints must not hide the answer.
        found = json.loads(out.stdout.strip().splitlines()[-1]) if out.returncode == 0 and out.stdout.strip() else None
        detail = out.stderr.strip()[-600:]
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        found, detail = None, str(e)[-600:]
    if not isinstance(found, dict) or not {"prefix", "python"} <= set(found):
        raise ArlError(f"python does not start in the environment {cfg.env_prefix}" + (f":\n{detail}" if detail else "."))
    if os.path.realpath(found["prefix"]) != os.path.realpath(prefix):
        raise ArlError(f"A job's python is {found['prefix']}, not the environment the contract names ({cfg.env_prefix}).")
    return {"python": found["python"], "prefix": cfg.env_prefix}


def shell_python_elsewhere(ledger: Ledger) -> str | None:
    """Where 'python' of the calling shell lives, when that is not the contract's environment."""
    prefix = env_prefix(ledger)
    if prefix is None:
        return None
    mine = shutil.which("python")
    if mine and os.path.realpath(os.path.dirname(mine)) == os.path.realpath(prefix / "bin"):
        return None
    return mine or "not on the PATH"


_HARNESS: str | None = None


def harness_version() -> str:
    """The arl that is running: its version, and its commit when it runs from its own checkout."""
    global _HARNESS
    if _HARNESS is None:
        text, root = __version__, Path(__file__).resolve().parents[2]
        try:
            git = Git(root)
            if git.top().resolve() == root:
                text += f" ({git.head()[:10]}{', modified' if git.tracked_changes() else ''})"
        except (ArlError, OSError):
            pass
        _HARNESS = text
    return _HARNESS


# --- conditions -------------------------------------------------------------------


def own_listing(ledger: Ledger) -> str | None:
    """Every package the python of a job would see, asked of that python: name and
    version, for an editable install its folder, commit and whether it was changed,
    and for a conda environment what conda installed, with the build. None when
    there is no python to ask: a run does not depend on this."""
    try:
        out = subprocess.run(["bash", "-c", 'exec python -I -c "$0"', _LISTING], cwd=ledger.home, env=job_env(ledger), capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 and out.stdout.startswith("python==") else None


def command_listing(ledger: Ledger) -> str | None:
    """The output of [env] command, when the contract has one."""
    cmd = ledger.cfg.env_command
    if not cmd:
        return None
    out = subprocess.run(["bash", "-c", cmd], cwd=ledger.home, env=job_env(ledger), capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise ArlError(f"[env] command failed: {cmd}\n{out.stderr.strip()[-2000:]}")
    return out.stdout


LISTS = ("listing", "packages")  # arl's own list of what python sees, and the output of [env] command


def snapshot(ledger: Ledger, probed: dict[str, Any] | None = None, store: bool = True) -> tuple[dict[str, Any], dict[str, str]]:
    """Where and with what a run happens: what goes into a record, and the package lists as text.

    probed: what probe_env answered, when the contract names an environment.
    store: keep the lists under research/envs/, once per distinct content. A command
    that only looks leaves it out.
    """
    info: dict[str, Any] = {
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "arl": harness_version(),
        "arl_python": sys.version.split()[0],
    }
    info.update({k: probed[k] for k in ("prefix", "python") if probed and k in probed})
    gpu = _nvidia("name,driver_version")
    if gpu:
        info["gpu"] = [row.strip() for row in gpu]
    texts = {key: text for key, text in (("listing", own_listing(ledger)), ("packages", command_listing(ledger))) if text is not None}
    for key, text in texts.items():
        info[key] = sha256_text(text)[:16]
        if store:
            ledger.env_root.mkdir(parents=True, exist_ok=True)
            path = ledger.env_root / f"{info[key]}.txt"
            if not path.exists():
                path.write_text(text)
    return info, texts


def environment(ledger: Ledger, probed: dict[str, Any] | None = None) -> dict[str, Any]:
    """What goes into a record about the environment. The package lists are stored with it."""
    return snapshot(ledger, probed)[0]


def stored_listing(ledger: Ledger, env: dict[str, Any] | None, key: str = "listing") -> str | None:
    """A package list a record points at, if it is still in the ledger."""
    digest = (env or {}).get(key)
    path = ledger.env_root / f"{digest}.txt" if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{8,64}", digest) else None
    try:
        return path.read_text() if path is not None and path.exists() else None
    except OSError:
        return None


def _name(text: str) -> str:
    return re.sub(r"[-_.]+", "-", text.strip()).lower()


def _packages(text: str) -> dict[str, str]:
    """A package list as {name: what the list says about it}. arl's own lines and the
    lines pip freeze writes are understood; any other line stands for itself."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("conda: ") and line.count("=") >= 2:
            name, version, build = line[7:].rsplit("=", 2)
            out[f"conda {_name(name)}"] = f"{version} {build}"
        elif line.startswith("-e "):
            rest = line[3:].strip()
            if "#egg=" in rest:
                where, name = rest.rsplit("#egg=", 1)
                tail = where.split("://", 1)[-1]
                out[_name(name.split("&")[0])] = f"editable at {tail.rsplit('@', 1)[1][:12]}" if "@" in tail else f"editable from {where}"
            else:
                out[_name(os.path.basename(rest.rstrip("/")) or rest)] = f"editable in {rest}"
        elif "==" in line:
            name, said = line.split("==", 1)
            out[_name(name)] = said.strip()
        elif " @ " in line:
            name, said = line.split(" @ ", 1)
            out[_name(name)] = f"from {said.strip()}"
        else:
            out[line] = ""
    return out


def listing_changes(old: str, new: str) -> list[str]:
    """What differs between two package lists, one line per package. What conda says about
    a package is left out where the line of python already says the same."""
    before, after = _packages(old), _packages(new)

    def conda_repeats(key: str) -> bool:
        base = key[6:]
        if not key.startswith("conda ") or (base not in before and base not in after):
            return False
        if key in before and key in after:  # say it only when the build changed and the version did not
            return before[key].split()[0] != after[key].split()[0]
        return (base in before) == (key in before) and (base in after) == (key in after)

    def order(k: str) -> tuple[int, bool, str]:  # what changed first, then what is new, then what is gone
        return (0 if k in before and k in after else 1 if k in after else 2, k.startswith("conda "), k)

    out = []
    for k in sorted((k for k in {*before, *after} if before.get(k) != after.get(k) and not conda_repeats(k)), key=order):
        if k in before and k in after:
            out.append(f"{k} {before[k] or 'listed'} -> {after[k] or 'listed'}")
        else:
            said = after[k] if k in after else before[k]
            out.append(f"{k}{' ' + said if said else ''} {'added' if k in after else 'removed'}")
    return out


def env_differences(ledger: Ledger, old: dict[str, Any] | None, new: dict[str, Any] | None, new_texts: dict[str, str] | None = None) -> list[str]:
    """Everything two recorded environments differ in that could change a result:
    the machine, the system, the GPU and its driver, python, and the package lists.

    new_texts: the lists of the newer one, when they are at hand and not stored.
    Never raises: a record from an older arl, or a list that is gone, says less.
    """
    old, new, out = old or {}, new or {}, []
    for key, label in (("host", "machine"), ("platform", "system"), ("prefix", "environment"), ("python", "python")):
        if old.get(key) and new.get(key) and old[key] != new[key]:
            out.append(f"{label} {old[key]} -> {new[key]}")
    if old.get("gpu") and new.get("gpu") and old["gpu"] != new["gpu"]:
        out.append(f"GPU and driver {'; '.join(map(str, old['gpu']))} -> {'; '.join(map(str, new['gpu']))}")
    both = [key for key in LISTS if old.get(key) and new.get(key)]
    if both:
        key = both[0]  # arl's own list when both runs have it: one format, every package with its version
        if old[key] != new[key]:
            before = stored_listing(ledger, old, key)
            after = (new_texts or {}).get(key) or stored_listing(ledger, new, key)
            if before is None or after is None:
                out.append("the package lists differ, and one of them is no longer in the ledger")
            else:
                said = any(line.startswith("python ") for line in out)
                out += [c for c in listing_changes(before, after) if not (said and c.startswith("python "))]
    elif any(old.get(key) for key in LISTS) and any(new.get(key) for key in LISTS):
        out.append("the package lists of the two are of different kinds and cannot be compared (one is arl's own, the other the output of [env] command)")
    return out


def differences(n: int) -> str:
    return f"{n} difference{'' if n == 1 else 's'}"


def short(changes: list[str], first: int = 6) -> str:
    """A list of differences in one line: the first few, and how many more."""
    more = len(changes) - first
    return "; ".join(changes[:first]) + (f"; and {more} more" if more > 0 else "")


def read_train_metrics(path: Path) -> tuple[dict[str, float], str]:
    """What a run reports about itself ({out}/train_metrics.json): numbers, kept as they
    are and never compared. Returns them and, when the file is no good, why."""
    if not path.exists():
        return {}, ""
    try:
        data = json.loads(read_text(path))
    except (ArlError, OSError, json.JSONDecodeError) as e:
        return {}, f"{path.name} is not JSON ({e}): not recorded"
    if not isinstance(data, dict):
        return {}, f"{path.name} must hold one JSON object: not recorded"
    numbers = {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}
    return dict(list(numbers.items())[:200]), ""


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
