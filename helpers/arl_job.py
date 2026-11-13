"""Saving state in a job that arl runs, so the run can be paused and continued.

Copy this file next to your run script. It needs nothing but the standard
library, and it also works without arl (then it never resumes).

What arl tells a job, and what this file does with it:

- ARL_OUT: the output folder of the run. State goes into <out>/checkpoint/,
  which arl never scores and never copies into the ledger.
- ARL_RESUME=1: an earlier part of this run was paused or cut off. Read the
  state and go on from there. With 0 the run starts from nothing.
- ARL_CHECKPOINT_EVERY: seconds between two saves. Saving after every step
  wastes time, never saving loses the whole run to a pause.
- SIGTERM: arl asks the job to stop (a pause, a closed terminal, the machine
  low on memory). Save and leave. After [run] pause_grace_s it is killed.

    from arl_job import Job

    job = Job()
    state = job.load() or {"step": 0}
    for step in range(state["step"], steps):
        ...                                  # one unit of work
        state["step"] = step + 1
        if job.due():                        # the interval has passed, or a stop was asked for
            job.save(state)
        if job.stopping:
            job.leave()                      # ends the process, arl records a pause

State that is not JSON (arrays, model weights) is written with job.write:

    def arrays(tmp):
        with open(tmp, "wb") as f:
            np.savez(f, **partial)

    job.write(arrays, "partial.npz")
    ...
    path = job.saved("partial.npz")          # None when the run starts from nothing

Save only at points where the state is whole: between two units of work, never
in the middle of one. A signal only sets a flag; nothing is cut short.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable


class Job:
    def __init__(self, out: str | os.PathLike | None = None, every: float | None = None, name: str = "state.json"):
        out = out or os.environ.get("ARL_OUT")
        if not out:
            raise SystemExit("arl_job: no output folder. Pass one, or run through arl, which sets ARL_OUT.")
        self.dir = Path(out) / "checkpoint"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.every = float(every if every is not None else os.environ.get("ARL_CHECKPOINT_EVERY") or 600)
        self.resuming = os.environ.get("ARL_RESUME") == "1"
        self.stopping = False
        self.saves = 0
        self._last = time.monotonic()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._asked)

    def _asked(self, signum: int, frame: Any) -> None:
        self.stopping = True  # only a flag: the loop saves at its next safe point

    def path(self, name: str | None = None) -> Path:
        return self.dir / (name or self.name)

    def saved(self, name: str | None = None) -> Path | None:
        """The file an earlier part of this run saved, or None when the run starts from nothing."""
        path = self.path(name)
        return path if self.resuming and path.exists() else None

    def load(self, name: str | None = None) -> Any:
        """The JSON state an earlier part saved, or None."""
        path = self.saved(name)
        return json.loads(path.read_text()) if path else None

    def due(self) -> bool:
        """Time to save: the interval has passed, or the job was asked to stop."""
        return self.stopping or time.monotonic() - self._last >= self.every

    def save(self, state: Any, name: str | None = None) -> None:
        """Write the state as JSON."""
        self.write(lambda tmp: tmp.write_text(json.dumps(state)), name)

    def write(self, writer: Callable[[Path], Any], name: str | None = None) -> None:
        """writer(path) writes the state. The file gets its name only once it is complete,
        so a reader finds the old state or the new one, never half of one."""
        path = self.path(name)
        tmp = path.with_name(f".{path.name}.tmp")
        writer(tmp)
        os.replace(tmp, path)
        self.saves += 1
        self._last = time.monotonic()

    def leave(self, code: int = 143) -> None:
        """End the process after the state is saved.

        The code is not 0: when arl asked for the stop it records a pause whatever the
        code, and when something else did, the run must not pass for a finished one.
        """
        sys.stdout.flush()
        sys.exit(code)
