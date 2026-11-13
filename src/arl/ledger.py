"""The ledger: every record under research/ and where each one lives.

    research/
        arl.toml, PROGRAM.md, eval/...   the contract (frozen, see contract.py)
        frozen.lock                      hashes of the contract
        IDEAS.md                         ideas outside the current path
        STATUS.md                        generated summary
        hypotheses/H003-slug.md
        tracks/<name>.md, <name>.params.json
        experiments/E0007-slug/          record.json, design.md, params.json, analysis.md, ...
        reviews/R002.md
        envs/<hash>.txt                  environment listings, stored once each

The ledger lives in the project's home checkout. Code lives on track branches,
each with its own worktree.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time
from pathlib import Path
from typing import Any, Generator

from . import config as config_mod
from .gitops import Git
from .util import ArlError, now, read_front, read_json, write_front, write_json

CONTRACT_FILES = ("arl.toml", "PROGRAM.md")
LOCK = "frozen.lock"
IDEA_STATES = ("open", "promoted", "parked", "rejected")
HYP_STATES = ("open", "supported", "refuted", "inconclusive", "dropped")
_IDEA_HEAD = re.compile(r"^## (I\d{3}): (.*)$", re.M)


class Ledger:
    def __init__(self, home: Path, name: str = "research"):
        self.home = Path(home).resolve()
        self.name = name
        self.dir = self.home / name
        self.git = Git(self.home)
        self._cfg: config_mod.Config | None = None

    @classmethod
    def locate(cls, start: Path | None = None) -> "Ledger":
        """ARL_HOME if set, else the main worktree of the repository around the cwd.

        Called from inside a track worktree it still finds the home checkout, so
        the stale copy of research/ on a track branch is never written.
        """
        name = os.environ.get("ARL_DIR", "research")
        if os.environ.get("ARL_HOME"):
            return cls(Path(os.environ["ARL_HOME"]), name)
        start = Path(start or Path.cwd())
        try:
            home = Git(start).main_worktree()
        except (ArlError, IndexError) as e:
            raise ArlError(f"{start} is not inside a git repository") from e
        return cls(home, name)

    # --- paths -------------------------------------------------------------------

    @property
    def config_path(self) -> Path:
        return self.dir / "arl.toml"

    @property
    def program_path(self) -> Path:
        return self.dir / "PROGRAM.md"

    @property
    def ideas_path(self) -> Path:
        return self.dir / "IDEAS.md"

    @property
    def status_path(self) -> Path:
        return self.dir / "STATUS.md"

    @property
    def lock_path(self) -> Path:
        return self.dir / LOCK

    @property
    def exp_root(self) -> Path:
        return self.dir / "experiments"

    @property
    def hyp_root(self) -> Path:
        return self.dir / "hypotheses"

    @property
    def track_root(self) -> Path:
        return self.dir / "tracks"

    @property
    def review_root(self) -> Path:
        return self.dir / "reviews"

    @property
    def env_root(self) -> Path:
        return self.dir / "envs"

    @property
    def cache_root(self) -> Path:
        return self.dir / ".cache"

    def rel(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.home).as_posix()

    @property
    def cfg(self) -> config_mod.Config:
        if self._cfg is None:
            self._cfg = config_mod.load(self.config_path)
        return self._cfg

    def exists(self) -> bool:
        return self.config_path.exists()

    # --- git ---------------------------------------------------------------------

    def check_home(self) -> None:
        branch, want = self.git.branch(), self.cfg.home_branch
        if want and branch != want:
            raise ArlError(
                f"{self.home} is on branch '{branch or 'detached'}', but the ledger lives on '{want}'."
            )
        email = self.cfg.git_email
        if email and self.git.email() != email:
            raise ArlError(
                f"git user.email here is '{self.git.email()}', arl.toml expects '{email}'. Not committing."
            )

    def pathspecs(self) -> list[str]:
        """Everything under research/ except the contract, which only 'arl freeze' commits."""
        specs = [self.name]
        specs += [f":(exclude){self.name}/{f}" for f in (*CONTRACT_FILES, LOCK)]
        specs += [f":(exclude){p.rstrip('/')}" for p in self.cfg.frozen]
        return specs

    def commit(self, message: str) -> str | None:
        self.check_home()
        return self.git.commit_paths(f"{self.cfg.commit_prefix}: {message}", self.pathspecs())

    @contextlib.contextmanager
    def locked(self, wait: float = 120.0) -> Generator[None, None, None]:
        """One writer at a time. Waits a while for the other one."""
        if not self.exists():
            raise ArlError(f"no {self.config_path}. Run 'arl init' in the project first.")
        self.check_home()
        with open(self.dir / ".arl.lock", "w") as f:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if time.monotonic() > deadline:
                        raise ArlError("another arl command is writing this ledger, try again") from e
                    time.sleep(0.2)
            yield

    # --- ids ---------------------------------------------------------------------

    def next_id(self, kind: str) -> str:
        if kind == "E":
            used = [int(p.name[1:5]) for p in self.exp_root.glob("E[0-9][0-9][0-9][0-9]*")]
            return f"E{max(used, default=0) + 1:04d}"
        if kind == "H":
            used = [int(p.name[1:4]) for p in self.hyp_root.glob("H[0-9][0-9][0-9]*.md")]
            return f"H{max(used, default=0) + 1:03d}"
        if kind == "R":
            used = [int(p.name[1:4]) for p in self.review_root.glob("R[0-9][0-9][0-9]*.md")]
            return f"R{max(used, default=0) + 1:03d}"
        if kind == "I":
            used = [int(i["id"][1:]) for i in self.ideas()]
            return f"I{max(used, default=0) + 1:03d}"
        raise ValueError(kind)

    # --- experiments -------------------------------------------------------------

    def exp_dir(self, eid: str) -> Path:
        matches = sorted(self.exp_root.glob(f"{eid}-*")) + sorted(self.exp_root.glob(eid))
        if not matches:
            raise ArlError(f"no experiment {eid}")
        return matches[0]

    def record(self, eid: str) -> dict[str, Any]:
        return read_json(self.exp_dir(eid) / "record.json")

    def save_record(self, rec: dict[str, Any]) -> None:
        write_json(self.exp_dir(rec["id"]) / "record.json", rec)

    def experiments(self) -> list[dict[str, Any]]:
        if not self.exp_root.exists():
            return []
        paths = sorted(self.exp_root.glob("E[0-9][0-9][0-9][0-9]*/record.json"))
        return [read_json(p) for p in paths]

    # --- hypotheses --------------------------------------------------------------

    def hyp_path(self, hid: str) -> Path:
        matches = sorted(self.hyp_root.glob(f"{hid}-*.md")) + sorted(self.hyp_root.glob(f"{hid}.md"))
        if not matches:
            raise ArlError(f"no hypothesis {hid}")
        return matches[0]

    def hypothesis(self, hid: str) -> tuple[dict[str, Any], str]:
        return read_front(self.hyp_path(hid))

    def hypotheses(self) -> list[dict[str, Any]]:
        if not self.hyp_root.exists():
            return []
        return [read_front(p)[0] for p in sorted(self.hyp_root.glob("H[0-9][0-9][0-9]*.md"))]

    def set_hypothesis(self, hid: str, status: str, note: str = "") -> None:
        if status not in HYP_STATES:
            raise ArlError(f"hypothesis status must be one of {', '.join(HYP_STATES)}")
        path = self.hyp_path(hid)
        meta, body = read_front(path)
        if meta.get("status") != status or note:
            meta["status"], meta["updated"] = status, now()
            if note:
                body = body.rstrip("\n") + f"\n- {now()[:10]}: {status}. {note}\n"
            write_front(path, meta, body)

    # --- tracks ------------------------------------------------------------------

    def track_path(self, name: str) -> Path:
        return self.track_root / f"{name}.md"

    def track(self, name: str) -> dict[str, Any]:
        path = self.track_path(name)
        if not path.exists():
            raise ArlError(f"no track '{name}'. Tracks: {', '.join(t['name'] for t in self.tracks()) or 'none'}")
        return read_front(path)[0]

    def tracks(self) -> list[dict[str, Any]]:
        if not self.track_root.exists():
            return []
        return [read_front(p)[0] for p in sorted(self.track_root.glob("*.md"))]

    def save_track(self, meta: dict[str, Any], body: str | None = None) -> None:
        path = self.track_path(meta["name"])
        if body is None:
            body = read_front(path)[1]
        write_front(path, meta, body)

    def track_params_path(self, name: str) -> Path:
        return self.track_root / f"{name}.params.json"

    def worktree_root(self) -> Path:
        root = Path(os.path.expanduser(self.cfg.worktrees))
        return root if root.is_absolute() else (self.home / root).resolve()

    def worktree_path(self, track: str) -> Path:
        return self.worktree_root() / f"{self.cfg.name}-{track}"

    # --- reviews -----------------------------------------------------------------

    def review_path(self, rid: str) -> Path:
        path = self.review_root / f"{rid}.md"
        if not path.exists():
            raise ArlError(f"no review {rid}")
        return path

    def review(self, rid: str) -> tuple[dict[str, Any], str]:
        return read_front(self.review_path(rid))

    def reviews(self) -> list[dict[str, Any]]:
        if not self.review_root.exists():
            return []
        return [read_front(p)[0] for p in sorted(self.review_root.glob("R[0-9][0-9][0-9].md"))]

    # --- ideas -------------------------------------------------------------------

    def _idea_sections(self) -> tuple[str, list[tuple[str, str, str]]]:
        """(text before the first idea, [(id, title, section text)])."""
        if not self.ideas_path.exists():
            return "# Ideas\n\n", []
        text = self.ideas_path.read_text()
        heads = list(_IDEA_HEAD.finditer(text))
        if not heads:
            return text, []
        sections = []
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            sections.append((m.group(1), m.group(2).strip(), text[m.start() : end]))
        return text[: heads[0].start()], sections

    def ideas(self) -> list[dict[str, str]]:
        out = []
        for iid, title, section in self._idea_sections()[1]:
            status = re.search(r"^Status:\s*(\w+)", section, re.M)
            out.append({"id": iid, "title": title, "status": status.group(1) if status else "open"})
        return out

    def add_idea(self, title: str, source: str, kind: str, body: str) -> str:
        iid = self.next_id("I")
        head, sections = self._idea_sections()
        entry = (
            f"## {iid}: {title}\n"
            f"Status: open\n"
            f"Added: {now()[:10]}. From: {source or 'not given'}. Kind: {kind}.\n\n"
            f"{body.strip()}\n\n"
        )
        text = head + "".join(s for _, _, s in sections)
        if not text.endswith("\n\n"):
            text = text.rstrip("\n") + "\n\n"
        self.ideas_path.write_text(text + entry)
        return iid

    def set_idea(self, iid: str, status: str, note: str) -> None:
        if status not in IDEA_STATES:
            raise ArlError(f"idea status must be one of {', '.join(IDEA_STATES)}")
        head, sections = self._idea_sections()
        if iid not in [s[0] for s in sections]:
            raise ArlError(f"no idea {iid} in {self.ideas_path}")
        out = []
        for sid, _, section in sections:
            if sid == iid:
                if re.search(r"^Status:.*$", section, re.M):
                    section = re.sub(r"^Status:.*$", f"Status: {status}", section, count=1, flags=re.M)
                else:
                    first, _, rest = section.partition("\n")
                    section = f"{first}\nStatus: {status}\n{rest}"
                section = section.rstrip("\n") + f"\n- {now()[:10]}: {status}. {note}\n\n"
            out.append(section)
        self.ideas_path.write_text(head + "".join(out))
