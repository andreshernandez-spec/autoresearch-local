"""The ledger: every record under research/ and where each one lives.

    research/
        arl.toml, PROGRAM.md, eval/...   the contract (frozen, see contract.py)
        frozen.lock                      hashes of the contract
        KNOWLEDGE.md                     what the work has shown about the problem, distilled
        LESSONS.md                       what was learned about how to work in this project
        IDEAS.md                         ideas outside the current path
        STATUS.md                        generated summary
        activity.jsonl                   refused commands and notes, read at introspections
        PAUSED                           there while the loop is paused, with the handoff note
        hypotheses/H003-slug.md
        tracks/<name>.md, <name>.params.json
        experiments/E0007-slug/          record.json, design.md, params.json, analysis.md, ...
        introspections/N002.md
        envs/<hash>.txt                  package listings of the environment, stored once each

The ledger lives in the project's home checkout. Code lives on track branches,
each with its own worktree. Lessons that hold for any project live next to the
skill (skill/LESSONS.md beside this code), on this machine only: that file is
not tracked, so nothing a loop writes ends up in the arl repository.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
import tomllib
from pathlib import Path
from typing import Any, Generator

from . import config as config_mod
from .gitops import Git
from .util import (
    ArlError,
    canonical,
    check_id,
    now,
    one_line,
    read_front,
    read_json,
    read_text,
    sha256_file,
    sha256_text,
    text_block,
    write_front,
    write_json,
    write_text,
)

CONTRACT_FILES = ("arl.toml", "PROGRAM.md")
LOCK = "frozen.lock"
IDEA_STATES = ("open", "promoted", "parked", "rejected")
HYP_STATES = ("open", "supported", "refuted", "inconclusive", "dropped")
SCOPES = ("general", "project")
TEMPLATES = Path(__file__).parent / "templates"
GENERAL_LESSONS = Path(__file__).resolve().parents[2] / "skill" / "LESSONS.md"
_IDEA_HEAD = re.compile(r"^## (I\d{3}): (.*)$", re.M)
_IDEA_LIKE = re.compile(r"^## I\S*.*$", re.M)
_LESSON_HEAD = {"general": re.compile(r"^## (G\d{3}): (.*)$", re.M), "project": re.compile(r"^## (L\d{3}): (.*)$", re.M)}
_NEEDS = {
    "hypothesis": ("id", "title", "status"),
    "track": ("name", "status", "branch", "head_commit"),
    "introspection": ("id", "status"),
}


def _front(path: Path, kind: str) -> tuple[dict[str, Any], str]:
    """Front matter of a ledger file, or a message that says how to mend it."""
    try:
        meta, body = read_front(path)
        missing = [k for k in _NEEDS[kind] if k not in meta]
        why = f"no {', '.join(missing)}" if missing else ""
    except (ArlError, tomllib.TOMLDecodeError) as e:
        meta, body, why = {}, "", str(e)
    if why:
        raise ArlError(
            f"the block between the +++ lines at the top of {path} is missing or damaged ({why}).\n"
            "arl keeps its fields there; write only below it. If the file was committed,\n"
            f"'git diff -- {path}' shows the block that was lost. If it was new, remove the\n"
            "file, create it again with arl and paste your text back."
        )
    return meta, body


class Ledger:
    def __init__(self, home: Path, name: str = "research"):
        self.home = Path(home).resolve()
        self.name = name
        self.dir = self.home / name
        self.git = Git(self.home)
        self._cfg: config_mod.Config | None = None

    @classmethod
    def locate(cls, start: Path | None = None) -> "Ledger":
        """The main worktree of the repository around the current directory.

        Called from inside a track worktree it still finds the home checkout, so
        the stale copy of research/ on a track branch is never written.
        """
        try:
            start = Path(start or Path.cwd())
        except FileNotFoundError as e:
            raise ArlError("the current directory no longer exists (a branch switch removed it). cd to the project first.") from e
        try:
            home = Git(start).main_worktree()
        except (ArlError, IndexError, OSError) as e:
            raise ArlError(f"{start} is not inside a git repository") from e
        return cls(home)

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
    def knowledge_path(self) -> Path:
        return self.dir / "KNOWLEDGE.md"

    @property
    def lessons_path(self) -> Path:
        return self.dir / "LESSONS.md"

    @property
    def activity_path(self) -> Path:
        return self.dir / "activity.jsonl"

    @property
    def paused_path(self) -> Path:
        return self.dir / "PAUSED"

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
    def intro_root(self) -> Path:
        return self.dir / "introspections"

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

    def check_files(self) -> None:
        """Read every record once, so a damaged file stops a command before it changes anything."""
        self.hypotheses()
        self.tracks()
        self.introspections()
        self.ideas()
        self.experiments()
        self.lessons("project")
        self.paused()

    def ensure_files(self) -> None:
        """The knowledge and lessons files of a project that was set up before they existed."""
        for path, name in ((self.knowledge_path, "KNOWLEDGE.md"), (self.lessons_path, "LESSONS.md")):
            if not path.exists():
                write_text(path, read_text(TEMPLATES / name))

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
            self.ensure_files()
            self.check_files()
            yield

    # --- ids ---------------------------------------------------------------------

    def next_id(self, kind: str) -> str:
        if kind == "E":
            used = [int(p.name[1:5]) for p in self.exp_root.glob("E[0-9][0-9][0-9][0-9]*")]
            return f"E{max(used, default=0) + 1:04d}"
        if kind == "H":
            used = [int(p.name[1:4]) for p in self.hyp_root.glob("H[0-9][0-9][0-9]*.md")]
            return f"H{max(used, default=0) + 1:03d}"
        if kind == "N":
            used = [int(p.name[1:4]) for p in self.intro_root.glob("N[0-9][0-9][0-9]*.md")]
            return f"N{max(used, default=0) + 1:03d}"
        if kind == "I":
            used = [int(i["id"][1:]) for i in self.ideas()]
            return f"I{max(used, default=0) + 1:03d}"
        raise ValueError(kind)

    # --- experiments -------------------------------------------------------------

    def exp_dir(self, eid: str) -> Path:
        check_id("E", eid)
        matches = sorted(self.exp_root.glob(f"{eid}-*")) + sorted(self.exp_root.glob(eid))
        if not matches:
            raise ArlError(f"no experiment {eid}")
        return matches[0]

    @staticmethod
    def _read_record(path: Path) -> dict[str, Any]:
        try:
            return read_json(path)
        except json.JSONDecodeError as e:
            raise ArlError(f"{path} is damaged ({e}). arl writes that file; restore it from git.") from e

    def record(self, eid: str) -> dict[str, Any]:
        path = self.exp_dir(eid) / "record.json"
        if not path.exists():
            raise ArlError(
                f"{path.parent} has no record.json, so it is not an experiment. Remove the folder if it is a leftover."
            )
        return self._read_record(path)

    def save_record(self, rec: dict[str, Any]) -> None:
        write_json(self.exp_dir(rec["id"]) / "record.json", rec)

    def experiments(self) -> list[dict[str, Any]]:
        if not self.exp_root.exists():
            return []
        paths = sorted(self.exp_root.glob("E[0-9][0-9][0-9][0-9]*/record.json"))
        return [self._read_record(p) for p in paths]

    # --- hypotheses --------------------------------------------------------------

    def hyp_path(self, hid: str) -> Path:
        check_id("H", hid)
        matches = sorted(self.hyp_root.glob(f"{hid}-*.md")) + sorted(self.hyp_root.glob(f"{hid}.md"))
        if not matches:
            raise ArlError(f"no hypothesis {hid}")
        return matches[0]

    def hypothesis(self, hid: str) -> tuple[dict[str, Any], str]:
        return _front(self.hyp_path(hid), "hypothesis")

    def hypotheses(self) -> list[dict[str, Any]]:
        if not self.hyp_root.exists():
            return []
        return [_front(p, "hypothesis")[0] for p in sorted(self.hyp_root.glob("H[0-9][0-9][0-9]*.md"))]

    def set_hypothesis(self, hid: str, status: str, note: str) -> None:
        """Change a hypothesis's status and say why in its log. Doing it twice changes nothing."""
        if status not in HYP_STATES:
            raise ArlError(f"hypothesis status must be one of {', '.join(HYP_STATES)}")
        path = self.hyp_path(hid)
        meta, body = _front(path, "hypothesis")
        line = f"{status}. {' '.join(note.split())}"
        if meta.get("status") == status and line in body:
            return
        meta["status"], meta["updated"] = status, now()
        write_front(path, meta, body.rstrip("\n") + f"\n- {now()[:10]}: {line}\n")

    def log_hypothesis(self, hid: str, text: str) -> None:
        """A line in a hypothesis's log that leaves its status as it is."""
        path = self.hyp_path(hid)
        meta, body = _front(path, "hypothesis")
        text = " ".join(text.split())
        if text not in body:
            write_front(path, meta, body.rstrip("\n") + f"\n- {now()[:10]}: {text}\n")

    # --- tracks ------------------------------------------------------------------

    def track_path(self, name: str) -> Path:
        return self.track_root / f"{name}.md"

    def track(self, name: str) -> dict[str, Any]:
        path = self.track_path(name)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or "") or not path.exists():
            raise ArlError(f"no track '{name}'. Tracks: {', '.join(t['name'] for t in self.tracks()) or 'none'}")
        return _front(path, "track")[0]

    def tracks(self) -> list[dict[str, Any]]:
        if not self.track_root.exists():
            return []
        return [_front(p, "track")[0] for p in sorted(self.track_root.glob("*.md"))]

    def save_track(self, meta: dict[str, Any], body: str | None = None) -> None:
        path = self.track_path(meta["name"])
        if body is None:
            body = _front(path, "track")[1]
        write_front(path, meta, body)

    def track_params_path(self, name: str) -> Path:
        return self.track_root / f"{name}.params.json"

    def track_params(self, track: dict[str, Any]) -> dict[str, Any]:
        """The track's current params. arl wrote them; a hand edit is refused."""
        path = self.track_params_path(track["name"])
        try:
            params = read_json(path)
        except (OSError, json.JSONDecodeError) as e:
            raise ArlError(f"{path} is not valid JSON ({e}). arl writes that file; restore it from git.") from e
        if not isinstance(params, dict):
            raise ArlError(f"{path} must hold one JSON object. arl writes that file; restore it from git.")
        recorded = track.get("params_sha256")
        if recorded and sha256_text(canonical(params)) != recorded:
            raise ArlError(
                f"{path} was changed by hand. A track's params move only when an experiment is kept:\n"
                f"put the file back ('git diff -- {path}'), and change params in an experiment's own params.json."
            )
        return params

    def write_track_params(self, track: dict[str, Any], params: dict[str, Any]) -> None:
        """Write the params file and note its hash in the track's fields (the caller saves the track)."""
        write_json(self.track_params_path(track["name"]), params)
        track["params_sha256"] = sha256_text(canonical(params))

    def worktree_root(self) -> Path:
        root = Path(os.path.expanduser(self.cfg.worktrees))
        return root if root.is_absolute() else (self.home / root).resolve()

    def worktree_path(self, track: str) -> Path:
        return self.worktree_root() / f"{self.cfg.name}-{track}"

    # --- introspections ----------------------------------------------------------

    def intro_path(self, nid: str) -> Path:
        check_id("N", nid)
        path = self.intro_root / f"{nid}.md"
        if not path.exists():
            raise ArlError(f"no introspection {nid}")
        return path

    def introspection(self, nid: str) -> tuple[dict[str, Any], str]:
        return _front(self.intro_path(nid), "introspection")

    def introspections(self) -> list[dict[str, Any]]:
        if not self.intro_root.exists():
            return []
        return [_front(p, "introspection")[0] for p in sorted(self.intro_root.glob("N[0-9][0-9][0-9].md"))]

    # --- knowledge ---------------------------------------------------------------

    def knowledge_hash(self) -> str:
        return sha256_text(read_text(self.knowledge_path)) if self.knowledge_path.exists() else ""

    # --- what was last reloaded ----------------------------------------------------

    def _reload_hashes(self) -> dict[str, str]:
        files = {"KNOWLEDGE.md": self.knowledge_path, "LESSONS.md": self.lessons_path, "the general lessons": GENERAL_LESSONS}
        return {name: sha256_file(path) if path.exists() else "" for name, path in files.items()}

    def mark_reloaded(self) -> None:
        """Remember what 'arl reload' just printed. Kept on this machine, not in git."""
        self.cache_root.mkdir(parents=True, exist_ok=True)
        write_json(self.cache_root / "reloaded.json", self._reload_hashes())

    def changed_since_reload(self) -> list[str]:
        """The knowledge and lessons files that are no longer what the last reload printed:
        a person corrected one, or another project added a general lesson."""
        path = self.cache_root / "reloaded.json"
        try:
            seen = read_json(path) if path.exists() else {}
        except (ArlError, json.JSONDecodeError):
            seen = {}
        if not isinstance(seen, dict) or not seen:
            return []  # never reloaded here: the start of a session asks for it, not this check
        return [name for name, digest in self._reload_hashes().items() if seen.get(name) != digest]

    # --- lessons -----------------------------------------------------------------

    def lessons_file(self, scope: str) -> Path:
        if scope not in SCOPES:
            raise ArlError(f"a lesson is 'general' (any project that uses arl) or 'project' (this one), not '{scope}'")
        return GENERAL_LESSONS if scope == "general" else self.lessons_path

    def lessons(self, scope: str) -> list[dict[str, str]]:
        path = self.lessons_file(scope)
        if not path.exists():
            return []
        return [{"id": m.group(1), "title": m.group(2).strip()} for m in _LESSON_HEAD[scope].finditer(read_text(path))]

    def add_lesson(self, scope: str, title: str, body: str, source: str) -> str:
        """Append a lesson to the instructions agents load. Lessons are only added, never edited here."""
        path = self.lessons_file(scope)
        title, body = one_line(title, "the title"), text_block(body, "the body")
        source = one_line(source, "the source") if source else "not given"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(f".{path.name}.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # the general file is shared by every project on the machine
            text = read_text(path) if path.exists() else read_text(TEMPLATES / ("LESSONS_general.md" if scope == "general" else "LESSONS.md"))
            used = [int(m.group(1)[1:]) for m in _LESSON_HEAD[scope].finditer(text)]
            lid = f"{'G' if scope == 'general' else 'L'}{max(used, default=0) + 1:03d}"
            entry = f"## {lid}: {title}\nAdded: {now()[:10]}. From: {source}.\n\n{body}\n"
            write_text(path, text.rstrip("\n") + "\n\n" + entry)
        return lid

    # --- activity: refused commands and notes -------------------------------------

    def log_activity(self, kind: str, text: str, command: str = "", about: str = "") -> None:
        """One line in activity.jsonl. Never raises: the log must not break a command."""
        try:
            event = {"at": now(), "kind": kind, "text": text[:1200]}
            if command:
                event["command"] = command[:300]
            if about:
                event["about"] = about
            with open(self.activity_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except (OSError, ValueError):
            pass

    def activity(self) -> list[dict[str, Any]]:
        if not self.activity_path.exists():
            return []
        events = []
        for line in self.activity_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    # --- pausing the loop --------------------------------------------------------

    def paused(self) -> dict[str, Any] | None:
        """Who paused the loop, when, why, and what they left for whoever continues."""
        if not self.paused_path.exists():
            return None
        try:
            state = read_json(self.paused_path)
        except json.JSONDecodeError as e:
            raise ArlError(f"{self.paused_path} is damaged ({e}). Remove it only if the person who paused says so.") from e
        return state if isinstance(state, dict) else {}

    # --- ideas -------------------------------------------------------------------

    def _idea_sections(self) -> tuple[str, list[tuple[str, str, str]]]:
        """(text before the first idea, [(id, title, section text)])."""
        if not self.ideas_path.exists():
            return "# Ideas\n\n", []
        text = read_text(self.ideas_path)
        heads = list(_IDEA_HEAD.finditer(text))
        if not heads:
            return text, []
        sections = []
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            sections.append((m.group(1), m.group(2).strip(), text[m.start() : end]))
        return text[: heads[0].start()], sections

    def ideas(self) -> list[dict[str, str]]:
        """Every idea. One whose Status line arl cannot read counts as open, so it is not lost."""
        out = []
        for iid, title, section in self._idea_sections()[1]:
            found = re.search(r"^Status:\s*(\w+)", section, re.M)
            status = found.group(1).lower() if found else ""
            out.append({"id": iid, "title": title, "status": status if status in IDEA_STATES else "open"})
        return out

    def idea_problems(self) -> list[str]:
        """What a hand edit of IDEAS.md left that arl cannot read as meant."""
        if not self.ideas_path.exists():
            return []
        text, problems = read_text(self.ideas_path), []
        for m in _IDEA_LIKE.finditer(text):
            if not _IDEA_HEAD.fullmatch(m.group(0)):
                problems.append(f"IDEAS.md: '{m.group(0)[:60]}' is not read as an idea (a heading is '## I012: title')")
        ids = [s[0] for s in self._idea_sections()[1]]
        for iid in sorted({i for i in ids if ids.count(i) > 1}):
            problems.append(f"IDEAS.md has two entries called {iid}")
        for iid, _, section in self._idea_sections()[1]:
            found = re.search(r"^Status:\s*(\w+)", section, re.M)
            if not found or found.group(1).lower() not in IDEA_STATES:
                problems.append(f"IDEAS.md: {iid} has no Status line arl can read (one of {', '.join(IDEA_STATES)}); it counts as open")
        return problems

    def add_idea(self, title: str, source: str, kind: str, body: str) -> str:
        title, body = one_line(title, "the title"), text_block(body, "the body")
        source = one_line(source, "the source") if source else "not given"
        iid = self.next_id("I")
        head, sections = self._idea_sections()
        entry = (
            f"## {iid}: {title}\n"
            f"Status: open\n"
            f"Added: {now()[:10]}. From: {source}. Kind: {one_line(kind, 'the kind')}.\n\n"
            f"{body}\n\n"
        )
        text = head + "".join(s for _, _, s in sections)
        if not text.endswith("\n\n"):
            text = text.rstrip("\n") + "\n\n"
        write_text(self.ideas_path, text + entry)
        return iid

    def set_idea(self, iid: str, status: str, note: str) -> None:
        check_id("I", iid)
        if status not in IDEA_STATES:
            raise ArlError(f"idea status must be one of {', '.join(IDEA_STATES)}")
        head, sections = self._idea_sections()
        ids = [s[0] for s in sections]
        if iid not in ids:
            raise ArlError(f"no idea {iid} in {self.ideas_path}")
        if ids.count(iid) > 1:
            raise ArlError(f"{self.ideas_path} has two entries called {iid}. Give one of them another number first.")
        note = " ".join(note.split())
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
        write_text(self.ideas_path, head + "".join(out))
