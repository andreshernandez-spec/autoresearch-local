"""A toy project in a temporary git repository, driven through the real CLI.

The "method" returns params["x"] plus a constant in run.py. The evaluator scores
how close that is to a target it keeps to itself. Enough to exercise every gate.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from arl import cli  # noqa: E402
from arl.ledger import Ledger  # noqa: E402

RUN_PY = """\
import argparse, json, pathlib, sys, time

OFFSET = 0.0

p = argparse.ArgumentParser()
p.add_argument("--params"); p.add_argument("--out"); p.add_argument("--split")
a = p.parse_args()
params = json.load(open(a.params))
if params.get("crash"):
    sys.exit(3)
if params.get("hog_mb"):
    hog = b"x" * (params["hog_mb"] << 20)
if params.get("sleep"):
    time.sleep(params["sleep"])
x = params.get("x", 0.0) + OFFSET
pathlib.Path(a.out, "prediction.json").write_text(json.dumps({"x": x}))
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
keep = ["*.json"]

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

[review]
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
        cfg = dict(worktrees=str(self.base / "wt"), python=sys.executable, timeout=60, max_experiments=40, every=6, stagnation=4, extra="")
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

    def started(self, **over) -> str:
        """Contract frozen, one track, baseline at x = 0 kept. Returns a hypothesis id."""
        self.setup(**over)
        self.track()
        self.baseline()
        return self.hyp()


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
    monkeypatch.delenv("ARL_HOME", raising=False)
    monkeypatch.delenv("ARL_DIR", raising=False)
    return Project(root, tmp_path)
