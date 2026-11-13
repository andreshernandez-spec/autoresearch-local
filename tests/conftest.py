"""A toy project in a temporary git repository, driven through the real CLI.

The "method" returns params["x"] plus a constant in run.py. The evaluator scores
how close that is to a target it keeps to itself. Enough to exercise every gate.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from arl import cli, contract, runner  # noqa: E402
from arl import ledger as ledger_module  # noqa: E402
from arl.ledger import Ledger  # noqa: E402

DEAD_PID = 2**22 - 7
LONG_AGO = "2026-01-01T00:00:00+00:00"
NOTHING_NEW = ("--no-lessons", "nothing went wrong", "--knowledge-unchanged", "nothing new")

RUN_PY = """\
import argparse, json, os, pathlib, signal, sys, time

OFFSET = 0.0

p = argparse.ArgumentParser()
p.add_argument("--params"); p.add_argument("--out"); p.add_argument("--split")
a = p.parse_args()
params = json.load(open(a.params))
if params.get("crash"):
    sys.exit(3)
if params.get("hog_mb"):
    hog = b"x" * (params["hog_mb"] << 20)
if params.get("deaf"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pathlib.Path(a.out, "deaf.txt").write_text("")
if params.get("sleep"):
    time.sleep(params["sleep"])
x = params.get("x", 0.0) + OFFSET
out = pathlib.Path(a.out)
(out / "pid.txt").write_text(str(os.getpid()))
start = 0
if params.get("steps"):
    # A job that saves its state as it goes, stops when asked, and continues where it was.
    saved = out / "checkpoint" / "step.json"
    if os.environ.get("ARL_RESUME") == "1" and saved.exists():
        start = json.loads(saved.read_text())["step"]
    asked = []
    signal.signal(signal.SIGTERM, lambda *_: asked.append(1))
    saved.parent.mkdir(exist_ok=True)
    for step in range(start, params["steps"]):
        time.sleep(params.get("step_s", 0.1))
        saved.write_text(json.dumps({"step": step + 1}))
        if asked:
            print("asked to stop at step", step + 1)
            sys.exit(1)
if "report" in params:  # what a run says about itself: training loss, steps
    said = params["report"]
    (out / "train_metrics.json").write_text(said if isinstance(said, str) else json.dumps(said))
pathlib.Path(a.out, "prediction.json").write_text(json.dumps({"x": x, "continued_from": start}))
pathlib.Path(a.out, "weights.bin").write_bytes(b"0" * 4096)
print("predicted", x)
"""

EVAL_PY = """\
import argparse, json, pathlib

p = argparse.ArgumentParser()
p.add_argument("--out"); p.add_argument("--metrics"); p.add_argument("--split")
a = p.parse_args()
x = json.load(open(pathlib.Path(a.out, "prediction.json")))["x"]
target = {"dev": 0.7, "confirm": 0.72}[a.split]
json.dump({"score": 1 - abs(x - target), "cost": abs(x)}, open(a.metrics, "w"))
if a.split == "dev":
    pathlib.Path(a.out, "per_instance.csv").write_text("x,error\\n%r,%r\\n" % (x, abs(x - target)))
"""

ARL_TOML = """\
[project]
name = "toy"
worktrees = "{worktrees}"
home_branch = "main"
git_email = "test@example.org"

[run]
command = "{python} run.py --params {{params}} --out {{out}} --split {{split}}"
timeout_s = {timeout}
max_attempts = 2
checkpoint_every_s = 1
pause_grace_s = {grace}
keep = ["*.json", "*.csv"]

[eval]
command = "{python} research/eval/evaluate.py --out {{out}} --metrics {{metrics}} --split {{split}}"
timeout_s = 60
frozen = ["research/eval"]
splits = ["dev", "confirm"]

[[metric]]
name = "score"
goal = "max"
role = "primary"
min_delta = 0.01

[[metric]]
name = "cost"
goal = "min"
role = "constraint"
limit = 2.0

[budget]
max_experiments = {max_experiments}
{budget}

[introspection]
every = {every}
stagnation = {stagnation}
agent_may = ["continue", "adjust", "branch", "pivot"]
{extra}
"""

_FILL = re.compile(r"^\s*>?\s*FILL:.*$", re.M)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Project:
    def __init__(self, root: Path, base: Path):
        self.root, self.base = root, base
        self.research = root / "research"

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.root)

    def arl(self, *argv: str, code: int = 0) -> str:
        """Run a command in process. Returns stdout and stderr together."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            got = cli.main([str(a) for a in argv])
        text = out.getvalue()
        assert got == code, f"arl {' '.join(map(str, argv))} returned {got}, expected {code}:\n{text}"
        return text

    def fill(self, path: Path) -> None:
        path.write_text(_FILL.sub("Written for the test.", path.read_text()))

    def contract(self, **over) -> None:
        cfg = dict(worktrees=str(self.base / "wt"), python=sys.executable, timeout=60, max_experiments=40, every=6, stagnation=4, extra="", grace=5, budget="")
        cfg.update(over)
        (self.research / "arl.toml").write_text(ARL_TOML.format(**cfg))

    def setup(self, **over) -> None:
        self.arl("init")
        self.contract(**over)
        (self.research / "eval" / "evaluate.py").write_text(EVAL_PY)
        self.fill(self.research / "PROGRAM.md")
        self.arl("freeze", "--reason", "initial")

    def track(self, name: str = "main-line", *extra: str) -> Path:
        self.arl("track", "new", name, *extra)
        self.fill(self.research / "tracks" / f"{name}.md")
        return self.base / "wt" / f"toy-{name}"

    def hyp(self, title: str = "x nearer the target scores higher") -> str:
        out = self.arl("hyp", "new", "--title", title)
        hid = out.split(":")[0]
        self.fill(self.ledger.hyp_path(hid))
        return hid

    def draft(self, typ: str, track: str = "main-line", title: str = "", hyps: tuple[str, ...] = (), params: dict | None = None, offset: float | None = None, extra: tuple[str, ...] = ()) -> str:
        before = {r["id"] for r in self.ledger.experiments()}
        args = ["exp", "new", "--type", typ, "--track", track, "--title", title or f"{typ} run"]
        for h in hyps:
            args += ["--hyp", h]
        self.arl(*args, *extra)
        eid = next(r["id"] for r in self.ledger.experiments() if r["id"] not in before)
        exp = self.ledger.exp_dir(eid)
        self.fill(exp / "design.md")
        if params is not None:
            (exp / "params.json").write_text(json.dumps(params))
        if offset is not None:
            self.edit_code(track, offset)
            self.arl("exp", "commit", eid, "-m", f"set offset to {offset}")
        return eid

    def edit_code(self, track: str, offset: float) -> None:
        run = self.base / "wt" / f"toy-{track}" / "run.py"
        run.write_text(re.sub(r"OFFSET = .*", f"OFFSET = {offset}", run.read_text()))

    def judge(self, eid: str, verdict: str, *extra: str, code: int = 0) -> str:
        exp = self.ledger.exp_dir(eid)
        if (exp / "analysis.md").exists():
            self.fill(exp / "analysis.md")
        return self.arl("exp", "judge", eid, "--verdict", verdict, "--reason", "because of the test", *extra, code=code)

    def baseline(self, track: str = "main-line", params: dict | None = None) -> str:
        eid = self.draft("baseline", track, "baseline", params=params)
        self.arl("exp", "run", eid)
        self.judge(eid, "keep")
        return eid

    def tune(self, x: float, verdict: str, hyp: str, state: str = "open", track: str = "main-line") -> str:
        eid = self.draft("tune", track, f"x {x}", (hyp,), {"x": x})
        self.arl("exp", "run", eid)
        self.judge(eid, verdict, "--hyp", f"{hyp}={state}")
        return eid

    def record(self, eid: str) -> dict:
        return self.ledger.record(eid)

    def introspect(self, decision: str = "continue", *extra: str, reload: bool = True) -> str:
        """Open an introspection, write it, close it with nothing new to record, reload."""
        before = {n["id"] for n in self.ledger.introspections()}
        self.arl("introspect", "new", "--trigger", "asked")
        nid = next(n["id"] for n in self.ledger.introspections() if n["id"] not in before)
        self.fill(self.research / "introspections" / f"{nid}.md")
        flags = extra or NOTHING_NEW
        self.arl("introspect", "close", nid, "--decision", decision, *flags)
        if reload and self.ledger.introspection(nid)[0]["status"] == "closed":
            self.arl("reload")
        return nid

    def started(self, **over) -> str:
        """Contract frozen, one track, baseline at x = 0 kept. Returns a hypothesis id."""
        self.setup(**over)
        self.track()
        self.baseline()
        return self.hyp()


def cut_off(p: "Project", eid: str, child: dict | None = None) -> None:
    """Leave the record of a draft or a failed run as it is when the arl that ran it was
    killed in the middle: marked as running, its process gone."""
    lock = contract.load(p.ledger)
    rec = p.record(eid)
    wt = p.ledger.worktree_path(rec["track"])
    code = git(wt, "rev-parse", "HEAD")
    rec["attempts"].append({"n": len(rec["attempts"]) + 1, "started": LONG_AGO, "code_commit": code, "segments": [{"started": LONG_AGO}]})
    rec.update(status="running", pid=DEAD_PID, pid_start=1, code_commit=code)
    rec.setdefault("contract", {k: lock[k] for k in ("version", "digest", "eval_digest")})
    rec.setdefault("conditions", {"command": p.ledger.cfg.run_command, "split": "dev", "worktree": str(wt), "data": [], "data_digest": runner.data_digest([])})
    if child:
        rec["child"] = child
    p.ledger.save_record(rec)


def pause_record(p: "Project", eid: str, seconds: float = 1.0) -> None:
    """Leave the record of a draft as it is after a part of its run was paused."""
    cut_off(p, eid)
    rec = p.record(eid)
    attempt = rec["attempts"][-1]
    attempt["segments"][0].update(ended=LONG_AGO, ended_by="paused", seconds=seconds)
    attempt.update(paused="a pause was asked for", run_seconds=seconds)
    rec["status"] = "paused"
    for key in ("pid", "pid_start"):
        rec.pop(key)
    p.ledger.save_record(rec)


def make_env(path: Path) -> Path:
    """A real environment of its own (a venv without pip), for a contract that names one."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)], check=True, capture_output=True)
    return path


def install(env: Path, name: str, version: str, editable: Path | None = None) -> None:
    """Make the environment report a package at a version, the way an install or an upgrade would.
    editable: the folder an editable install points at."""
    site = next((env / "lib").glob("python*")) / "site-packages"
    for old in site.glob(f"{name}-*.dist-info"):
        shutil.rmtree(old)
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    if editable is not None:
        (info / "direct_url.json").write_text(json.dumps({"url": f"file://{editable}", "dir_info": {"editable": True}}))


def env_table(env: Path) -> str:
    """The [env] table of a contract that names this environment."""
    return f'[env]\nprefix = "{env}"\n'


LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "arl"


def launch(p: "Project", *argv: str) -> subprocess.Popen:
    """The real launcher in a process of its own, for what needs a signal or a second process."""
    return subprocess.Popen([sys.executable, str(LAUNCHER), *argv], cwd=p.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_for(condition, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


def job_pid(p: "Project", eid: str, folder: str = "run-1") -> int:
    """The pid the toy job wrote when it started."""
    path = p.ledger.exp_dir(eid) / "artifacts" / folder / "pid.txt"
    assert wait_for(lambda: path.exists() and path.read_text().strip()), f"the job of {eid} did not start"
    return int(path.read_text())


def saved_step(p: "Project", eid: str, folder: str = "run-1") -> int:
    """The step the toy job last saved, 0 before the first."""
    try:
        return json.loads((p.ledger.exp_dir(eid) / "artifacts" / folder / "checkpoint" / "step.json").read_text())["step"]
    except (OSError, ValueError, KeyError):
        return 0


def prediction(p: "Project", eid: str, folder: str = "run-1") -> dict:
    return json.loads((p.ledger.exp_dir(eid) / "artifacts" / folder / "prediction.json").read_text())


def pid_alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


@pytest.fixture
def project(tmp_path, monkeypatch) -> Project:
    root = tmp_path / "toy"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.org")
    git(root, "config", "user.name", "Test")
    (root / "run.py").write_text(RUN_PY)
    (root / ".gitignore").write_text("__pycache__/\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "toy project")
    monkeypatch.chdir(root)
    # general lessons belong to this repository: tests write theirs somewhere else
    monkeypatch.setattr(ledger_module, "GENERAL_LESSONS", tmp_path / "general" / "LESSONS.md")
    return Project(root, tmp_path)
