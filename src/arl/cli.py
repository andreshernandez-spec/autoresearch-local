"""Command line. Every change of the ledger goes through one of these commands.

Each command checks everything it can before it changes anything, so a refusal
leaves the ledger, the worktrees and git as they were.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sys
from pathlib import Path
from typing import Any

from . import contract, render, rules, runner
from .config import DECISIONS
from .gitops import Git
from .ledger import HYP_STATES, IDEA_STATES, Ledger
from .util import (
    EXIT_BUSY,
    EXIT_CRASH,
    ArlError,
    canonical,
    check_id,
    check_text,
    fmt,
    now,
    one_line,
    read_front,
    read_json,
    require_filled,
    sha256_file,
    sha256_text,
    slugify,
    write_front,
    write_json,
    write_text,
)

ARL = render.LAUNCHER
HYP_VERDICTS = ("supported", "refuted", "inconclusive", "open")


def interactive() -> bool:
    """A person at a terminal. Some decisions are only theirs."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def finish(ledger: Ledger, message: str) -> None:
    """Refresh STATUS.md and commit the ledger."""
    write_text(ledger.status_path, render.status(ledger))
    ledger.commit(message)


def _load_params(path: Path) -> dict[str, Any]:
    try:
        params = read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        raise ArlError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(params, dict):
        raise ArlError(f"{path} must hold one JSON object")
    return params


def _design_hashes(exp: Path) -> tuple[str, str]:
    design = exp / "design.md"
    if not design.exists():
        raise ArlError(f"{design} is missing. Restore it from git: the design is part of the record.")
    return sha256_file(design), sha256_text(canonical(_load_params(exp / "params.json")))


def _require_design_unchanged(ledger: Ledger, rec: dict[str, Any]) -> None:
    exp = ledger.exp_dir(rec["id"])
    if (rec["design"]["design_sha256"], rec["design"]["params_sha256"]) != _design_hashes(exp):
        raise ArlError(
            f"design.md or params.json of {rec['id']} changed after its run started. The design is locked:\n"
            f"put them back ('git -C {ledger.home} diff -- {ledger.rel(exp)}')."
        )


# --- gates ------------------------------------------------------------------------


def _gate_new_work(ledger: Ledger) -> None:
    """Reasons no new experiment may be drafted now."""
    rs = rules.review_state(ledger)
    if rs["due"]:
        raise ArlError(
            "A review comes first:\n  " + "\n  ".join(rs["reasons"]) + f"\nRun '{ARL} next' for the steps."
        )
    if rs["last_decision"] == "stop":
        raise ArlError(f"{rs['last']} decided to stop the program. Only a person can reopen it.")
    hit = rules.target_met(ledger)
    if hit:
        raise ArlError(f"{hit['id']} reached the target. Run '{ARL} next'.")
    spent = rules.budget(ledger)["spent"]
    if spent:
        raise ArlError("The budget is spent: " + "; ".join(spent))


def _gate_running(ledger: Ledger, first_attempt: bool) -> None:
    """Reasons nothing may be run now, even a draft that already exists."""
    rs = rules.review_state(ledger)
    if rs["waiting"]:
        raise ArlError(f"{rs['waiting']} waits for a person to decide. Nothing runs until then.")
    if rs["last_decision"] == "stop":
        raise ArlError(f"{rs['last']} decided to stop the program. Nothing runs until a person reopens it.")
    b = rules.budget(ledger)
    if b["hours_spent"] or (first_attempt and b["count_spent"]):
        raise ArlError("The budget is spent: " + "; ".join(b["spent"]))


def _authorized(ledger: Ledger, review_id: str | None, decisions: tuple[str, ...], what: str) -> str:
    """Who allows a change of direction: nobody is needed before the first verdict,
    after it the latest closed review, or a person at a terminal."""
    if not any(e["status"] == "judged" for e in ledger.experiments()):
        return "setup"
    if review_id:
        meta, _ = ledger.review(review_id)
        last = rules.review_state(ledger)["last"]
        if meta["status"] != "closed" or review_id != last:
            raise ArlError(f"{review_id} is not the latest closed review ({last or 'there is none yet'}). An older review does not decide today.")
        if decisions and meta.get("decision") not in decisions:
            raise ArlError(f"{review_id} decided '{meta.get('decision')}', not {' or '.join(decisions)}")
        return review_id
    if interactive():
        return "person"
    raise ArlError(
        f"Once experiments have been judged, {what} comes out of a review:\n"
        "pass --review <id> of the latest closed review"
        + (f" (it must have decided {' or '.join(decisions)})" if decisions else "")
        + ",\nor let a person do it in a terminal."
    )


def _worktree(ledger: Ledger, track: dict[str, Any]) -> tuple[Path, Git]:
    path = ledger.worktree_path(track["name"])
    if not path.exists():
        raise ArlError(
            f"the worktree of track {track['name']} is missing: {path}\n"
            f"Put it back with: {ARL} track worktree {track['name']}"
        )
    return path, Git(path)


def _exp_worktree(ledger: Ledger, rec: dict[str, Any]) -> tuple[Path, Git]:
    path, wgit = _worktree(ledger, ledger.track(rec["track"]))
    if wgit.branch() != rec["branch"]:
        raise ArlError(f"{path} is on '{wgit.branch()}', but {rec['id']} lives on '{rec['branch']}'")
    return path, wgit


def _fail(rec: dict[str, Any], reason: str) -> None:
    attempt = rec["attempts"][-1]
    attempt.setdefault("ended", now())
    attempt["failed"] = reason
    rec["status"] = "crashed"
    for key in ("pid", "pid_start", "child", "metrics", "comparison"):
        rec.pop(key, None)


def _settle(ledger: Ledger, rec: dict[str, Any], certain: bool = False) -> bool:
    """A record says 'running' but the arl that ran it is gone.

    Stop whatever the run left behind and count the attempt as failed.
    certain: the caller holds the run lock, so nothing can be running.
    """
    if rec["status"] != "running" or not (certain or rules.stale(rec)):
        return False
    note = "interrupted: arl died during the run"
    if rec.get("child") and runner.stop_session(rec["child"]):
        note += ", the processes it left behind were stopped"
    if not rec.get("attempts"):
        rec["attempts"] = [{"n": 1, "started": now()}]
    _fail(rec, note)
    ledger.save_record(rec)
    finish(ledger, f"{rec['id']} interrupted")
    return True


# --- setup ------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    if ledger.exists():
        raise ArlError(f"{ledger.config_path} already exists")
    for sub in ("eval", "hypotheses", "tracks", "experiments", "reviews"):
        (ledger.dir / sub).mkdir(parents=True, exist_ok=True)
    branch = ledger.git.branch() or "main"
    files = {
        ledger.program_path: render.template("PROGRAM.md"),
        ledger.ideas_path: render.template("IDEAS.md"),
        ledger.dir / ".gitignore": render.template("gitignore"),
        ledger.dir / "eval" / "README.md": render.template("eval_README.md"),
        ledger.config_path: render.template("arl.toml").replace("@NAME@", ledger.home.name).replace("@BRANCH@", branch),
    }
    kept = []
    for path, text in files.items():
        if path.exists():
            kept.append(path.name)  # never overwrite what is already there
        else:
            path.write_text(text)
    print(f"Created {ledger.dir}" + (f" (left as they were: {', '.join(kept)})" if kept else ""))
    print("Next: write PROGRAM.md and arl.toml, put the evaluator under eval/, then")
    print(f"  {ARL} freeze --reason \"initial\"")
    return 0


def cmd_freeze(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    person = interactive()
    old = contract.load(ledger) if ledger.exists() else None
    if old and person and not args.yes:
        # Ask before taking the ledger lock: a run that ends meanwhile must be able to write.
        print("Changes since contract v%d:" % old["version"])
        for line in contract.problems(ledger):
            print(f"  {line}")
        if input(f"Freeze these as contract v{old['version'] + 1}? [y/N] ").strip().lower() != "y":
            raise ArlError("not frozen")
    with ledger.locked():
        lock = contract.freeze(ledger, args.reason, person)
        write_text(ledger.status_path, render.status(ledger))
        ledger.git.commit_paths(f"{ledger.cfg.commit_prefix}: freeze contract v{lock['version']}", contract.commit_paths(ledger))
    print(f"Contract v{lock['version']} frozen ({len(lock['files'])} files).")
    if old and old["eval_digest"] != lock["eval_digest"]:
        print("The evaluator or the metrics changed: every track needs a new baseline.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    if args.json:
        print(json.dumps(render.state(ledger), indent=2, default=str))
    else:
        print(render.status(ledger), end="")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    if ledger.exists():
        ledger.check_files()  # a damaged record is the first thing to say
    action = rules.next_action(ledger)
    if args.json:
        print(json.dumps(action, indent=2))
    else:
        print(render.next_text(ledger, action), end="")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Check that the ledger, the contract and git still agree."""
    ledger = Ledger.locate()
    ledger.check_files()  # a record that cannot be read is reported on its own, first
    bad = list(contract.problems(ledger))
    git = ledger.git
    for t in ledger.tracks():
        if not git.branch_exists(t["branch"]):
            bad.append(f"track {t['name']}: branch {t['branch']} is gone")
        elif t["status"] == "active" and git.head(t["branch"]) != t["head_commit"]:
            bad.append(f"track {t['name']}: branch moved outside arl (run 'arl track sync {t['name']}')")
        try:
            ledger.track_params(t)
        except ArlError as e:
            bad.append(f"track {t['name']}: {str(e).splitlines()[0]}")
    for folder in sorted(ledger.exp_root.glob("E[0-9][0-9][0-9][0-9]*")) if ledger.exp_root.exists() else []:
        if not (folder / "record.json").exists():
            bad.append(f"{ledger.rel(folder)} has no record.json: a leftover, not an experiment")
    for e in ledger.experiments():
        exp = ledger.exp_dir(e["id"])
        if "design" in e:
            try:
                design_hash, params_hash = _design_hashes(exp)
                if design_hash != e["design"]["design_sha256"]:
                    bad.append(f"{e['id']}: design.md changed after the run started")
                if params_hash != e["design"]["params_sha256"]:
                    bad.append(f"{e['id']}: params.json changed after the run started")
            except ArlError as err:
                bad.append(f"{e['id']}: {str(err).splitlines()[0]}")
        if e.get("code_commit") and not git.commit_exists(e["code_commit"]):
            bad.append(f"{e['id']}: commit {e['code_commit'][:10]} is not in the repository")
        if rules.kept(e) and e["type"] == "feature":
            track = ledger.track(e["track"])
            if git.branch_exists(track["branch"]) and not git.is_ancestor(e["code_commit"], track["branch"]):
                bad.append(f"{e['id']}: kept, but its commit is not in {track['branch']}")
        if "metrics" in e:
            stored = read_json(exp / "metrics.json") if (exp / "metrics.json").exists() else {}
            if any(stored.get(k) != v for k, v in e["metrics"].items()):
                bad.append(f"{e['id']}: metrics.json differs from the record")
    ids = [i["id"] for i in ledger.ideas()]
    for iid in sorted({i for i in ids if ids.count(i) > 1}):
        bad.append(f"IDEAS.md has two entries called {iid}")
    if bad:
        print("\n".join(bad))
        return 1
    print("ok")
    return 0


# --- tracks -----------------------------------------------------------------------


def cmd_track_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    name = args.name
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", name):
        raise ArlError("a track name is lower case letters, digits and dashes")
    with ledger.locked():
        contract.require(ledger)
        if ledger.track_path(name).exists():
            raise ArlError(f"track {name} already exists")
        opened_by = _authorized(ledger, args.review, ("branch", "pivot"), "a new track")
        git, branch = ledger.git, f"track/{name}"
        if git.branch_exists(branch):
            raise ArlError(f"branch {branch} already exists")
        start = git.head(args.from_ref or "HEAD")
        path = ledger.worktree_path(name)
        if path.exists():
            raise ArlError(f"{path} already exists")
        params = _load_params(Path(args.params)) if args.params else {}
        path.parent.mkdir(parents=True, exist_ok=True)
        git.create_branch(branch, start)
        git.add_worktree(path, branch)
        ledger.track_root.mkdir(parents=True, exist_ok=True)
        meta = {
            "name": name,
            "status": "active",
            "branch": branch,
            "created": now(),
            "from_ref": args.from_ref or git.branch() or "HEAD",
            "base_commit": start,
            "head_commit": start,
            "head_experiment": "",
            "opened_by": opened_by,
        }
        ledger.write_track_params(meta, params)
        body = render.fill(render.template("track.md"), name=name, branch=branch, worktree=path)
        write_front(ledger.track_path(name), meta, body)
        finish(ledger, f"open track {name}")
    print(f"Track {name}: branch {branch} at {start[:10]}, worktree {path}")
    print(f"Now write {ledger.track_path(name)} (approach, premises, stop if).")
    return 0


def _track_log(ledger: Ledger, meta: dict[str, Any], line: str) -> None:
    body = read_front(ledger.track_path(meta["name"]))[1]
    ledger.save_track(meta, body.rstrip("\n") + f"\n- {now()[:10]}: {' '.join(line.split())}\n")


def cmd_track_set(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        contract.require(ledger)
        track = ledger.track(args.name)
        if track["status"] == args.status:
            raise ArlError(f"track {args.name} is already {args.status}")
        flying = rules.in_flight(ledger, args.name)
        if args.status != "active" and flying:
            raise ArlError(f"{flying[0]['id']} is still in flight on track {args.name}")
        by = _authorized(ledger, args.review, (), "parking, closing or reopening a track")
        track["status"] = args.status
        _track_log(ledger, track, f"{args.status} (by {by}). {args.note}")
        finish(ledger, f"track {args.name} {args.status}")
    print(f"Track {args.name} is {args.status}.")
    return 0


def cmd_track_sync(args: argparse.Namespace) -> int:
    """The branch was changed by hand. Record the new head; the track needs a new baseline."""
    ledger = Ledger.locate()
    with ledger.locked():
        contract.require(ledger)
        track = ledger.track(args.name)
        if rules.in_flight(ledger, args.name):
            raise ArlError(f"an experiment is in flight on track {args.name}")
        head = ledger.git.head(track["branch"])
        if head == track["head_commit"]:
            raise ArlError("the branch did not move")
        old = track["head_commit"]
        track["head_commit"], track["head_experiment"] = head, ""
        _track_log(ledger, track, f"branch moved outside arl from {old[:10]} to {head[:10]}. {args.note}")
        finish(ledger, f"track {args.name} synced to {head[:10]}")
    print(f"Track {args.name} now at {head[:10]}. It needs a new baseline.")
    return 0


def cmd_track_worktree(args: argparse.Namespace) -> int:
    """Put a track's worktree back when its folder is gone."""
    ledger = Ledger.locate()
    with ledger.locked():
        track = ledger.track(args.name)
        path = ledger.worktree_path(args.name)
        if path.exists():
            raise ArlError(f"{path} is there already")
        flying = rules.in_flight(ledger, args.name)
        branch = flying[0]["branch"] if flying else track["branch"]
        ledger.git.prune_worktrees()  # git still lists the folder that is gone
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger.git.add_worktree(path, branch)
    print(f"Track {args.name}: worktree {path} is back, on {branch}.")
    return 0


def cmd_track_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    print(render.track_table(ledger, contract.load(ledger)), end="")
    for t in ledger.tracks():
        print(f"{t['name']}: {ledger.worktree_path(t['name'])}")
    return 0


# --- hypotheses and ideas ---------------------------------------------------------


def cmd_hyp_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        hid = ledger.next_id("H")
        ledger.hyp_root.mkdir(parents=True, exist_ok=True)
        path = ledger.hyp_root / f"{hid}-{slugify(args.title)}.md"
        source = one_line(args.source, "--from") if args.source else ""
        meta = {"id": hid, "title": args.title, "status": "open", "created": now(), "updated": now(), "source": source}
        write_front(path, meta, render.fill(render.template("hypothesis.md"), id=hid, title=args.title))
    print(f"{hid}: write {path} (below the +++ block, which is arl's)")
    return 0


def cmd_hyp_set(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        ledger.set_hypothesis(args.id, args.status, args.note)
        finish(ledger, f"{args.id} {args.status}")
    print(f"{args.id} is {args.status}.")
    return 0


def cmd_hyp_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rows = [[h["id"], h["status"], h["title"]] for h in ledger.hypotheses() if not args.status or h["status"] == args.status]
    print(render.table(["id", "status", "title"], rows), end="")
    return 0


def cmd_idea_add(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        iid = ledger.add_idea(args.title, args.source or "", args.kind, args.body)
        if ledger.lock_path.exists():
            finish(ledger, f"idea {iid}")
    print(f"{iid} added to {ledger.ideas_path}")
    return 0


def cmd_idea_set(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        ledger.set_idea(args.id, args.status, args.note)
        finish(ledger, f"idea {args.id} {args.status}")
    print(f"{args.id} is {args.status}.")
    return 0


def cmd_idea_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rows = [[i["id"], i["status"], i["title"]] for i in ledger.ideas() if not args.status or i["status"] == args.status]
    print(render.table(["id", "status", "title"], rows), end="")
    return 0


# --- experiments ------------------------------------------------------------------


def cmd_exp_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    typ, hyps = args.type, [check_id("H", h) for h in (args.hyp or [])]
    with ledger.locked():
        lock = contract.require(ledger)
        cfg = ledger.cfg
        _gate_new_work(ledger)
        track = ledger.track(args.track)
        if track["status"] != "active":
            raise ArlError(f"track {args.track} is {track['status']}")
        require_filled(ledger.track_path(args.track), f"the description of track {args.track}")
        flying = rules.in_flight(ledger, args.track)
        if flying:
            raise ArlError(
                f"{flying[0]['id']} is still in flight on track {args.track} ({flying[0]['status']}). "
                "One experiment at a time per track: finish it first."
            )
        path, wgit = _worktree(ledger, track)
        if wgit.branch() != track["branch"]:
            raise ArlError(f"{path} is on '{wgit.branch()}', expected '{track['branch']}'")
        dirty = wgit.dirty()
        if dirty:
            raise ArlError(f"{path} has uncommitted changes:\n  " + "\n  ".join(dirty[:10]))
        head = wgit.head()
        if head != track["head_commit"]:
            raise ArlError(
                f"branch {track['branch']} moved outside arl ({track['head_commit'][:10]} -> {head[:10]}).\n"
                f"Record it with '{ARL} track sync {args.track} --note \"...\"', then run a baseline."
            )
        params = ledger.track_params(track)
        if "params_sha256" not in track:  # a track from before the hash was kept: trust it once
            ledger.write_track_params(track, params)
            ledger.save_track(track)
        if typ == "baseline" and hyps:
            raise ArlError("a baseline measures the track as it is. It tests no hypothesis: leave --hyp out.")
        for h in hyps:
            meta, _ = ledger.hypothesis(h)
            require_filled(ledger.hyp_path(h), f"hypothesis {h}")
            if meta["status"] == "dropped":
                raise ArlError(f"{h} was dropped")
        if typ in ("tune", "feature"):
            if not hyps:
                raise ArlError(f"a {typ} experiment tests a hypothesis: write one with 'arl hyp new' and pass --hyp")
            if rules.reference_for(ledger, track, lock["eval_digest"]) is None:
                raise ArlError(f"track {args.track} has no reference under contract v{lock['version']}: run a baseline first")
        if args.command and typ != "diagnose":
            raise ArlError("--command is for diagnose experiments. Scored runs use the command in arl.toml.")

        eid, slug = ledger.next_id("E"), slugify(args.title)
        exp = ledger.exp_root / f"{eid}-{slug}"
        branch = f"exp/{eid}-{slug}" if typ in ("feature", "diagnose") else track["branch"]
        if exp.exists() or (branch != track["branch"] and ledger.git.branch_exists(branch)):
            raise ArlError(f"{exp.name} or branch {branch} exists already: a leftover. Remove the folder, or pick another title.")
        rec = {
            "id": eid,
            "title": args.title,
            "type": typ,
            "track": args.track,
            "hypotheses": hyps,
            "status": "draft",
            "created": now(),
            "base_commit": head,
            "branch": branch,
            "command": args.command or "",
            "attempts": [],
        }
        ctx = {
            "id": eid,
            "title": args.title,
            "type": typ,
            "track": args.track,
            "hyps": ", ".join(hyps) or "no hypothesis",
            "primary": cfg.primary.name,
            "change_hint": "params only" if typ == "tune" else "code, and params if the code needs them",
        }
        kind = typ if typ in ("baseline", "diagnose") else "scored"
        design = render.fill(render.template(f"design_{kind}.md"), **ctx)
        # Everything is checked. From here on, undo on failure.
        exp.mkdir(parents=True)
        try:
            write_text(exp / "design.md", design)
            write_json(exp / "params.json", params)
            write_json(exp / "record.json", rec)
            if branch != track["branch"]:
                wgit.switch_new(branch, head)
        except BaseException:
            shutil.rmtree(exp, ignore_errors=True)
            raise
        write_text(ledger.status_path, render.status(ledger))
    print(render.next_text(ledger, {"stage": "design", "target": eid, "why": f"{eid} drafted on track {args.track}"}), end="")
    return 0


def _blob_id(path: Path) -> str:
    """The id git gives this file's content (sha1 object format)."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def cmd_exp_commit(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        rec = ledger.record(args.id)
        if rec["type"] not in ("feature", "diagnose"):
            raise ArlError(f"{rec['id']} is a {rec['type']} experiment: it does not change code")
        _settle(ledger, rec)
        if rec["status"] not in ("draft", "crashed"):
            raise ArlError(f"{rec['id']} is {rec['status']}: its code is fixed")
        path, wgit = _exp_worktree(ledger, rec)
        dirty = wgit.dirty()
        if not dirty:
            raise ArlError(f"nothing to commit in {path}")
        # research/ belongs to the home checkout. The only change allowed there is one
        # that puts a file back to what the track started from.
        base, wrong = rec["base_commit"], []
        for rel in (p for p in dirty if p.startswith(f"{ledger.name}/")):
            before, here = wgit.blob(base, rel), path / rel
            same = (not here.exists() and before is None) or (here.is_file() and before == _blob_id(here))
            if not same:
                wrong.append(rel)
        if wrong:
            raise ArlError(
                f"{ledger.name}/ belongs to the home checkout ({ledger.home}), not to a track worktree.\n"
                "Undo these changes first:\n  " + "\n  ".join(wrong[:10])
            )
        sha = wgit.commit_all(f"{rec['id']}: {one_line(args.message, 'the message')}")
    print(f"{rec['id']}: committed {sha[:10] if sha else ''} on {rec['branch']} ({len(dirty)} paths)")
    return 0


def _run_env(ledger: Ledger, rec: dict[str, Any], values: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        ARL_HOME=str(ledger.home),
        ARL_RESEARCH=str(ledger.dir),
        ARL_EXP=str(values["exp"]),
        ARL_OUT=str(values["out"]),
        ARL_PARAMS=str(values["params"]),
        ARL_SPLIT=str(values["split"]),
        ARL_ID=rec["id"],
        ARL_TRACK=rec["track"],
    )
    return env


def cmd_exp_run(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    eid = check_id("E", args.id)
    # The run lock is held from the checks to the end: no second run can start in
    # between, and a record that says "running" while we hold it is a leftover.
    with runner.run_lock(ledger):
        with ledger.locked():
            lock = contract.require(ledger)
            rec = ledger.record(eid)
            cfg = ledger.cfg
            _settle(ledger, rec, certain=True)
            if rec["status"] not in ("draft", "crashed"):
                raise ArlError(f"{eid} is {rec['status']}. To run it again use '{ARL} exp reproduce {eid}'.")
            attempts = rec["attempts"]
            if len(attempts) >= cfg.max_attempts:
                raise ArlError(
                    f"{eid} has failed {len(attempts)} times, the limit. Judge it:\n"
                    f"  {ARL} exp judge {eid} --verdict crash --reason \"...\""
                )
            _gate_running(ledger, first_attempt=not attempts)
            exp = ledger.exp_dir(eid)
            require_filled(exp / "design.md", f"the design of {eid}")
            params = _load_params(exp / "params.json")
            for h in rec["hypotheses"]:
                require_filled(ledger.hyp_path(h), f"hypothesis {h}")
            track = ledger.track(rec["track"])
            path, wgit = _exp_worktree(ledger, rec)
            dirty = wgit.dirty()
            if dirty:
                how = f"Commit them with '{ARL} exp commit {eid} -m \"...\"' or undo them." if rec["type"] in ("feature", "diagnose") else "Undo them: this kind of experiment does not change code."
                raise ArlError(
                    f"{path} has uncommitted changes, so the run would not be the committed code:\n  "
                    + "\n  ".join(dirty[:10])
                    + f"\n{how}"
                )
            code, base, typ = wgit.head(), rec["base_commit"], rec["type"]
            again = "Judge it as a crash and draft again." if attempts else f"Abandon it ('{ARL} exp abandon {eid} --reason \"...\"') and draft again."
            if typ in ("baseline", "tune"):
                if code != track["head_commit"]:
                    raise ArlError(f"track {rec['track']} moved since {eid} was drafted. {again}")
            else:
                if not wgit.is_ancestor(base, code):
                    raise ArlError(f"{rec['branch']} no longer contains the commit it started from ({base[:10]})")
                if typ == "feature" and code == base:
                    raise ArlError(f"no code change is committed on {rec['branch']}. Edit, then '{ARL} exp commit {eid} -m \"...\"'.")
                touched = wgit.changed(base, code, ledger.name)
                if touched:
                    raise ArlError(
                        f"the code of {eid} changes {ledger.name}/, which belongs to the home checkout:\n  "
                        + "\n  ".join(touched[:10])
                        + f"\nPut those files back in the worktree, then '{ARL} exp commit {eid} -m \"...\"'."
                    )
            if typ == "baseline" and canonical(params) != canonical(ledger.track_params(track)):
                raise ArlError(
                    f"a baseline measures the track as it is, but {exp / 'params.json'} differs from the track's params.\n"
                    f"Put it back, or make the change a tune experiment. {again}"
                )
            design_hash, params_hash = _design_hashes(exp)
            if "design" in rec:
                _require_design_unchanged(ledger, rec)
            scored = typ in rules.SCORED
            split = cfg.splits[0]
            command = rec.get("command") or cfg.run_command
            what = {"track": rec["track"], "code": code, "params": params_hash, "command": command, "eval": lock["eval_digest"] if scored else "", "split": split}
            key = sha256_text(canonical(what))
            for other in ledger.experiments():
                same = other["id"] != eid and other.get("dedup") == key and other["status"] in ("evaluated", "judged")
                if same and other.get("verdict", {}).get("decision") != "crash":
                    if not args.allow_duplicate:
                        raise ArlError(
                            f"{eid} has the same code, params and evaluator as {other['id']}: the result is already known.\n"
                            "To repeat it on purpose (to see the noise), pass --allow-duplicate \"why\"."
                        )
                    rec["duplicate_of"], rec["duplicate_reason"] = other["id"], args.allow_duplicate
            reference = None
            if typ in ("tune", "feature"):
                reference = rules.reference_for(ledger, track, lock["eval_digest"])
                if reference is None:
                    raise ArlError(f"track {rec['track']} has no reference under contract v{lock['version']}. {again} Then run a baseline first.")
            reasons = runner.busy(cfg)
            if reasons:
                raise ArlError(
                    "The machine is busy, not starting:\n  " + "\n  ".join(reasons) + "\nWait and try again. Leave other jobs alone.",
                    EXIT_BUSY,
                )

            # All checks passed. Mark it as running and lock the design; if that
            # cannot be committed, nothing ran and no attempt is counted.
            before = json.dumps(rec)
            n = len(attempts) + 1
            out, logs = exp / "artifacts" / f"run-{n}", exp / "logs"
            values = {"params": exp / "params.json", "out": out, "split": split, "home": ledger.home, "research": ledger.dir, "exp": exp, "worktree": path, "metrics": exp / "metrics.json"}
            try:
                rec.update(status="running", pid=os.getpid(), pid_start=runner.proc_start(os.getpid()), code_commit=code, dedup=key)
                rec["contract"] = {k: lock[k] for k in ("version", "digest", "eval_digest")}
                rec.setdefault("design", {"design_sha256": design_hash, "params_sha256": params_hash, "locked_at": now()})
                rec["conditions"] = {
                    "command": command,
                    "split": split,
                    "worktree": str(path),
                    "env": runner.environment(ledger),
                    "data": runner.data_fingerprint(ledger),
                }
                attempts.append({"n": n, "started": now(), "code_commit": code})
                ledger.save_record(rec)
                finish(ledger, f"{eid} design, {typ} on {rec['track']}" if n == 1 else f"{eid} attempt {n}")
            except BaseException:
                ledger.save_record(json.loads(before))
                raise

        out.mkdir(parents=True, exist_ok=True)
        attempt, env, failed = attempts[-1], _run_env(ledger, rec, values), ""
        limits = {"max_rss_gb": cfg.max_run_rss_gb, "min_free_ram_gb": cfg.stop_below_free_ram_gb}

        def started(pid: int) -> None:
            rec["child"] = pid  # so a later arl can stop the job if this one is killed
            ledger.save_record(rec)

        try:
            res = runner.execute(runner.substitute(command, values), path, env, cfg.run_timeout, logs / f"run-{n}.log", on_start=started, **limits)
            attempt.update(run_seconds=res["seconds"], exit_code=res["exit_code"])
            write_text(exp / "run.tail.txt", runner.tail(logs / f"run-{n}.log"))
            if res["stopped"]:
                failed = f"the run was stopped: {res['stopped']}"
            elif res["exit_code"] != 0:
                failed = f"the run exited with code {res['exit_code']}"
            elif wgit.branch() != rec["branch"] or wgit.head() != code or wgit.tracked_changes():
                failed = "the code in the worktree changed while the run was going, so the result does not belong to the recorded commit"
            if not failed:
                rec["artifacts"] = runner.manifest(out)  # what the run left, before the evaluator adds to it
                if scored and not rec["artifacts"]:
                    failed = "the run wrote nothing into its output folder"
            if not failed and scored:
                lock_now = contract.require(ledger)  # the run itself must not have touched the evaluator
                if lock_now["digest"] != rec["contract"]["digest"]:
                    failed = f"the contract was frozen anew while the run was going (v{rec['contract']['version']} to v{lock_now['version']}), so this run cannot be scored"
            if not failed and scored:
                (exp / "metrics.json").unlink(missing_ok=True)
                ev = runner.execute(runner.substitute(cfg.eval_command, values), ledger.home, env, cfg.eval_timeout, logs / f"eval-{n}.log", on_start=started, **limits)
                attempt["eval_seconds"] = ev["seconds"]
                write_text(exp / "eval.tail.txt", runner.tail(logs / f"eval-{n}.log"))
                if ev["stopped"]:
                    failed = f"the evaluator was stopped: {ev['stopped']}"
                elif ev["exit_code"] != 0:
                    failed = f"the evaluator exited with code {ev['exit_code']}"
                elif contract.require(ledger)["digest"] != rec["contract"]["digest"]:
                    failed = "the contract was frozen anew while the evaluator was running, so this score cannot be trusted"
                else:
                    rec["metrics"] = runner.read_metrics(exp / "metrics.json", cfg)
                    rec["comparison"] = rules.compare(cfg, rec["metrics"], reference)
        except ArlError as e:
            failed = str(e)
        except BaseException:
            _fail(rec, "interrupted")
            ledger.save_record(rec)
            try:
                with ledger.locked():
                    finish(ledger, f"{eid} interrupted")
            except ArlError:
                pass  # the record is saved; the next arl command commits it
            raise

        def write_outcome() -> str:
            attempt["ended"] = now()
            for key in ("pid", "pid_start", "child"):
                rec.pop(key, None)
            if failed:
                _fail(rec, failed)
                ledger.save_record(rec)
                return f"{eid} failed, attempt {n}"
            rec["status"] = "evaluated"
            rec["kept"] = runner.keep_small(out, exp / "kept", cfg.keep, cfg.keep_max_kb)
            kind = "scored" if scored else "diagnose"
            facts = render.analysis_facts(ledger, rec)
            write_text(exp / "analysis.md", render.fill(render.template(f"analysis_{kind}.md"), id=eid, facts=facts))
            ledger.save_record(rec)
            return f"{eid} result" + (f", {cfg.primary.name} {fmt(rec['metrics'][cfg.primary.name])}" if scored else "")

        try:
            with ledger.locked(wait=900):
                finish(ledger, write_outcome())
        except ArlError as e:
            # The run is over and its result must not be lost because git or the home
            # checkout is in the way: save the record, leave the commit for later.
            if rec["status"] == "running":
                write_outcome()
            print(f"{eid}: the outcome is saved but could not be committed:\n  {e}\nFix that; the next arl command that writes the ledger commits it.", file=sys.stderr)

    if failed:
        print(f"{eid} FAILED (attempt {n} of {cfg.max_attempts}): {failed}")
        print(f"Log: {ARL} exp log {eid}")
        return EXIT_CRASH
    print(_summary(ledger, rec))
    print()
    print(render.next_text(ledger, {"stage": "judge", "target": eid, "why": f"{eid} has a result and no verdict"}), end="")
    return 0


def _summary(ledger: Ledger, rec: dict[str, Any]) -> str:
    cfg = ledger.cfg
    lines = [f"{rec['id']} {rec['title']} [{rec['type']} on {rec['track']}, {rec['status']}]"]
    if "metrics" in rec:
        comp = rec["comparison"]
        for m in cfg.metrics:
            if m.name in rec["metrics"]:  # a metric added to the contract later is not in older records
                lines.append(f"  {m.name:28s} {fmt(rec['metrics'][m.name])}" + ("  (primary)" if m.role == "primary" else ""))
        if comp.get("reference"):
            lines.append(
                f"  against {comp['reference']}: {comp['metric']} {fmt(comp['reference_value'])} -> {fmt(comp['value'])}, "
                f"gain {comp['gain']:+.6g} (at least {fmt(comp['min_delta'])} counts)"
            )
        for g in comp["gate_failures"]:
            lines.append(f"  constraint not met: {g}")
        lines.append(f"  outcome: {comp['outcome']}")
    if rec["status"] in ("judged", "abandoned"):
        v = rec["verdict"]
        lines.append(f"  verdict: {v['decision']}. {v['reason']}")
    return "\n".join(lines)


def cmd_exp_judge(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    eid, verdict = check_id("E", args.id), args.verdict
    with ledger.locked():
        contract.require(ledger)
        rec = ledger.record(eid)
        _settle(ledger, rec)
        if rec["status"] not in ("evaluated", "crashed"):
            raise ArlError(f"{eid} is {rec['status']}: there is nothing to judge")
        allowed = rules.allowed_verdicts(rec)
        outcome = rec.get("comparison", {}).get("outcome")
        if verdict not in allowed:
            what = outcome or ("not scored" if rec["status"] == "evaluated" else rec["status"])
            raise ArlError(f"{eid} is '{what}': the verdict can be {' or '.join(allowed)}, not {verdict}")
        if args.simpler and not (verdict == "keep" and outcome == "neutral"):
            raise ArlError("--simpler is only for keeping a neutral result")
        if verdict == "keep" and outcome == "neutral" and not args.simpler:
            raise ArlError("a neutral result is kept only when it makes things simpler: pass --simpler and say how in --reason")
        exp = ledger.exp_dir(eid)
        if rec["status"] == "evaluated":
            require_filled(exp / "analysis.md", f"the analysis of {eid}")
            _require_design_unchanged(ledger, rec)
        updates: dict[str, str] = {}
        for item in args.hyp or []:
            hid, _, state = item.partition("=")
            if hid not in rec["hypotheses"] or state not in HYP_VERDICTS:
                raise ArlError(f"--hyp takes <id>=<{'|'.join(HYP_VERDICTS)}> for a hypothesis of {eid}, got '{item}'")
            ledger.hypothesis(hid)  # it must still be there, before anything is changed
            updates[hid] = state
        if verdict == "crash" and any(state != "open" for state in updates.values()):
            raise ArlError("a crash says nothing about a hypothesis: leave --hyp out, or pass <id>=open")
        if rec["status"] == "evaluated" and rec["type"] in ("tune", "feature"):
            missing = [h for h in rec["hypotheses"] if h not in updates]
            if missing:
                raise ArlError("say what this run did to each hypothesis: " + " ".join(f"--hyp {h}=<{'|'.join(HYP_VERDICTS)}>" for h in missing))

        track = ledger.track(rec["track"])
        path, wgit = _worktree(ledger, track)
        changed = wgit.tracked_changes()
        if changed:
            raise ArlError(
                f"tracked files in {path} differ from the commit that ran:\n  "
                + "\n  ".join(changed[:10])
                + "\nThese edits are not part of what was measured. Copy what you want to keep somewhere else,\n"
                + f"then put the files back: git -C {path} restore <file>"
            )
        code_branch = rec["type"] in ("feature", "diagnose")
        if code_branch and wgit.branch() != rec["branch"]:
            raise ArlError(f"{path} is on '{wgit.branch()}', expected '{rec['branch']}'")
        if verdict == "keep" and wgit.head() != rec["code_commit"]:
            raise ArlError(f"{rec['branch']} moved after the run ({rec['code_commit'][:10]} was measured). Keep needs the measured commit.")
        kept_params = _load_params(exp / "params.json") if verdict == "keep" else None

        # Everything is checked: move the branch, then write.
        moved = {"before": track["head_commit"]}
        if code_branch:
            wgit.switch(track["branch"])
            if verdict == "keep":
                wgit.fast_forward(rec["branch"])
        if verdict == "keep":
            assert kept_params is not None
            track["head_commit"], track["head_experiment"] = rec["code_commit"], eid
            ledger.write_track_params(track, kept_params)
            _track_log(ledger, track, f"{eid} kept ({rec['type']}): {rec['title']}")
        moved["after"] = track["head_commit"]
        rec["verdict"] = {"decision": verdict, "reason": " ".join(args.reason.split()), "at": now(), "track_head": moved}
        if args.simpler:
            rec["verdict"]["simpler"] = True
        rec["status"] = "judged"
        ledger.save_record(rec)
        for hid, state in updates.items():
            ledger.set_hypothesis(hid, state, f"{eid} ({verdict})")
        finish(ledger, f"{eid} {verdict}")
    print(_summary(ledger, rec))
    print()
    print(render.next_text(ledger, rules.next_action(ledger)), end="")
    return 0


def cmd_exp_abandon(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    eid = check_id("E", args.id)
    with ledger.locked():
        rec = ledger.record(eid)
        _settle(ledger, rec)
        if rec["status"] != "draft" or rec["attempts"]:
            how = f" It ran: judge it ('{ARL} exp judge {eid} --verdict crash --reason \"...\"')." if rec["status"] == "crashed" else ""
            raise ArlError(f"only a draft that never ran can be abandoned. {eid} is {rec['status']}.{how}")
        track = ledger.track(rec["track"])
        path, wgit = _worktree(ledger, track)
        if rec["branch"] != track["branch"] and wgit.branch() == rec["branch"]:
            if wgit.dirty():
                raise ArlError(f"{path} has uncommitted changes. Commit them to {rec['branch']} ('{ARL} exp commit {eid} -m \"...\"') or undo them first.")
            wgit.switch(track["branch"])
        rec["status"] = "abandoned"
        rec["verdict"] = {"decision": "abandoned", "reason": " ".join(args.reason.split()), "at": now()}
        ledger.save_record(rec)
        finish(ledger, f"{eid} abandoned")
    print(f"{eid} abandoned. Its branch, if any, stays in git.")
    return 0


def cmd_exp_show(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rec = ledger.record(args.id)
    if args.json:
        print(json.dumps(rec, indent=2))
        return 0
    exp = ledger.exp_dir(args.id)
    print(_summary(ledger, rec))
    print(f"  tests: {' '.join(rec['hypotheses']) or 'no hypothesis'}")
    print(f"  code: {rec.get('code_commit', rec['base_commit'])[:10]} on {rec['branch']} (track head was {rec['base_commit'][:10]})")
    try:
        print(f"  params: {json.dumps(_load_params(exp / 'params.json'))}")
    except ArlError as e:
        print(f"  params: {e}")
    for a in rec["attempts"]:
        took = f"run {a.get('run_seconds', 0):.0f} s" + (f", evaluation {a['eval_seconds']:.0f} s" if "eval_seconds" in a else "")
        print(f"  attempt {a['n']}: {took}" + (f", FAILED: {a['failed']}" if a.get("failed") else ""))
    name = ledger.cfg.primary.name
    for r in rec.get("reruns", []):
        value = fmt(r["metrics"][name]) if name in r["metrics"] else "not reported"
        print(f"  rerun on {r['split']}: {name} {value}" + (f", reproduced: {r['reproduced']}" if "reproduced" in r else ""))
    print(f"  files: {exp}")
    return 0


def cmd_exp_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    exps = [
        e
        for e in ledger.experiments()
        if (not args.track or e["track"] == args.track) and (not args.status or e["status"] == args.status)
    ]
    print(render.exp_table(ledger, exps), end="")
    return 0


def cmd_exp_log(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    logs = ledger.exp_dir(args.id) / "logs"
    kind = "eval" if args.eval else "run"
    files = sorted(logs.glob(f"{kind}-*.log"), key=lambda p: p.stat().st_mtime) if logs.exists() else []
    if not files:
        raise ArlError(f"no {kind} log for {args.id} yet")
    print(f"# {files[-1]}")
    print(runner.tail(files[-1], lines=args.lines), end="")
    return 0


def cmd_exp_reproduce(args: argparse.Namespace) -> int:
    """Run a recorded experiment again from its commit and params, on any split."""
    ledger = Ledger.locate()
    eid = check_id("E", args.id)
    with runner.run_lock(ledger):
        with ledger.locked():
            lock = contract.require(ledger)
            rec = ledger.record(eid)
            cfg = ledger.cfg
            if rec["type"] not in rules.SCORED or "metrics" not in rec:
                raise ArlError("only a scored experiment with a result can be run again")
            _gate_running(ledger, first_attempt=False)
            _require_design_unchanged(ledger, rec)
            split = args.split or cfg.splits[0]
            if split not in cfg.splits:
                raise ArlError(f"arl.toml knows the splits {cfg.splits}, not '{split}'")
            reasons = runner.busy(cfg)
            if reasons:
                raise ArlError("The machine is busy, not starting:\n  " + "\n  ".join(reasons), EXIT_BUSY)
        exp = ledger.exp_dir(eid)
        n = len(rec.get("reruns", [])) + 1
        tree = ledger.worktree_root() / f"{cfg.name}-rerun-{eid}-{n}"
        out, logs = exp / "artifacts" / f"rerun-{n}-{split}", exp / "logs"
        out.mkdir(parents=True, exist_ok=True)
        metrics_path = out / "metrics.json"
        metrics_path.unlink(missing_ok=True)
        values = {"params": exp / "params.json", "out": out, "split": split, "home": ledger.home, "research": ledger.dir, "exp": exp, "worktree": tree, "metrics": metrics_path}
        env = _run_env(ledger, rec, values)
        tree.parent.mkdir(parents=True, exist_ok=True)
        if tree.exists():  # left by a rerun that was killed: it is arl's own throwaway tree
            try:
                ledger.git.remove_worktree(tree)
            except ArlError:
                shutil.rmtree(tree, ignore_errors=True)
        ledger.git.prune_worktrees()
        ledger.git.add_detached(tree, rec["code_commit"])
        limits = {"max_rss_gb": cfg.max_run_rss_gb, "min_free_ram_gb": cfg.stop_below_free_ram_gb}
        try:
            command = rec.get("command") or cfg.run_command
            res = runner.execute(runner.substitute(command, values), tree, env, cfg.run_timeout, logs / f"rerun-{n}.log", **limits)
            if res["stopped"] or res["exit_code"] != 0:
                why = res["stopped"] or f"exit {res['exit_code']}"
                raise ArlError(f"the rerun failed ({why}). Log: {logs / f'rerun-{n}.log'}", EXIT_CRASH)
            ev = runner.execute(runner.substitute(cfg.eval_command, values), ledger.home, env, cfg.eval_timeout, logs / f"rerun-eval-{n}.log", **limits)
            if ev["stopped"] or ev["exit_code"] != 0:
                why = ev["stopped"] or f"exit {ev['exit_code']}"
                raise ArlError(f"the evaluator failed on the rerun ({why}). Log: {logs / f'rerun-eval-{n}.log'}", EXIT_CRASH)
            if contract.require(ledger)["digest"] != lock["digest"]:
                raise ArlError("the contract was frozen anew during the rerun: run it again", EXIT_CRASH)
            metrics = runner.read_metrics(metrics_path, cfg)
        finally:
            ledger.git.remove_worktree(tree)
        entry: dict[str, Any] = {
            "n": n,
            "at": now(),
            "split": split,
            "metrics": metrics,
            "run_seconds": res["seconds"],
            "eval_seconds": ev["seconds"],
            "eval_digest": lock["eval_digest"],
        }
        primary = cfg.primary
        if split == cfg.splits[0] and rec["contract"]["eval_digest"] == lock["eval_digest"]:
            diff = abs(metrics[primary.name] - rec["metrics"][primary.name])
            entry["difference"] = diff
            entry["reproduced"] = bool(diff <= max(primary.min_delta, 1e-12))
        with ledger.locked(wait=900):
            rec = ledger.record(eid)
            rec.setdefault("reruns", []).append(entry)
            ledger.save_record(rec)
            finish(ledger, f"{eid} rerun on {split}")
    print(f"{eid} rerun on split '{split}': {primary.name} = {fmt(metrics[primary.name])}")
    if "reproduced" in entry:
        print(f"  recorded {fmt(rec['metrics'][primary.name])}, difference {entry['difference']:.3g}, reproduced: {entry['reproduced']}")
    else:
        print("  not compared with the recorded result (another split or another evaluator). Never used for a verdict.")
    return 0


# --- reviews ----------------------------------------------------------------------


def cmd_review_due(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rs = rules.review_state(ledger)
    if args.json:
        print(json.dumps(rs, indent=2))
    elif rs["due"]:
        print("A review is due:\n  " + "\n  ".join(rs["reasons"]))
    else:
        print(f"No review due: {rs['judged_since']} of {ledger.cfg.review_every} experiments judged since {rs['last'] or 'the start'}.")
    return 0


def cmd_review_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        contract.require(ledger)
        rs = rules.review_state(ledger)
        if rs["open"] or rs["waiting"]:
            raise ArlError(f"{rs['open'] or rs['waiting']} is not finished yet")
        pending = [e["id"] for e in ledger.experiments() if e["status"] in ("running", "evaluated")]
        if pending:
            raise ArlError(f"finish {', '.join(pending)} first: a review needs their verdicts")
        rid = ledger.next_id("R")
        trigger = one_line(args.trigger, "--trigger") if args.trigger else "; ".join(rs["reasons"]) or "asked for"
        ledger.review_root.mkdir(parents=True, exist_ok=True)
        meta = {"id": rid, "status": "open", "trigger": trigger, "opened": now(), "window": rs["window"]}
        body = render.fill(render.template("review.md"), id=rid, facts=render.review_facts(ledger, trigger, rs))
        write_front(ledger.review_root / f"{rid}.md", meta, body)
        write_text(ledger.status_path, render.status(ledger))
    print(f"{rid} opened: {ledger.review_root / f'{rid}.md'}")
    print("The facts are filled in. Write sections 1 to 7 with fresh eyes, below the +++ block, then:")
    print(f"  {ARL} review close {rid} --decision <{'|'.join(DECISIONS)}>")
    print(f"'{ARL} next' prints the full steps.")
    return 0


def cmd_review_close(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        contract.require(ledger)  # who may decide what is in arl.toml: it has to be the frozen one
        meta, body = ledger.review(args.id)
        if meta["status"] != "open":
            raise ArlError(f"{args.id} is {meta['status']}")
        require_filled(ledger.review_path(args.id), f"review {args.id}")
        reopening = rules.review_state(ledger)["last_decision"] == "stop" and args.decision != "stop"
        alone = args.decision in ledger.cfg.agent_may and not reopening
        meta.update(status="closed" if alone else "waiting", decision=args.decision, note=" ".join((args.note or "").split()))
        if alone:
            meta.update(closed=now(), decided_by="agent")
        else:
            meta["proposed"] = now()
        write_front(ledger.review_path(args.id), meta, body)
        finish(ledger, f"{args.id} review, {args.decision}" + ("" if alone else " proposed"))
    if alone:
        print(f"{args.id} closed: {args.decision}.")
    elif reopening:
        print(f"{args.id} proposes '{args.decision}' after a stop. Reopening the program is a person's decision.")
    else:
        print(f"{args.id} proposes '{args.decision}', which the agent may not decide alone.")
    print()
    print(render.next_text(ledger, rules.next_action(ledger)), end="")
    return 0


def cmd_review_resolve(args: argparse.Namespace) -> int:
    if not interactive():
        raise ArlError("a waiting review is resolved by a person, in a terminal")
    ledger = Ledger.locate()
    with ledger.locked():
        contract.require(ledger)
        meta, body = ledger.review(args.id)
        if meta["status"] != "waiting":
            raise ArlError(f"{args.id} is {meta['status']}, not waiting")
        note = " ".join(args.note.split())
        meta.update(status="closed", proposed_decision=meta["decision"], decision=args.decision, closed=now(), decided_by="person", note=note)
        write_front(ledger.review_path(args.id), meta, body.rstrip("\n") + f"\n\n## Resolved\n\n{now()[:10]}: {args.decision}. {note}\n")
        finish(ledger, f"{args.id} resolved, {args.decision}")
    print(f"{args.id} closed: {args.decision}.")
    return 0


# --- parser -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arl", description="Local harness for automated research.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def add(parent: Any, name: str, func: Any, help: str) -> argparse.ArgumentParser:
        sp = parent.add_parser(name, help=help, description=help)
        sp.set_defaults(func=func)
        return sp

    add(sub, "init", cmd_init, "Create research/ with templates in this project.")
    sp = add(sub, "freeze", cmd_freeze, "Hash the contract into frozen.lock. After the first experiment only a person can.")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--yes", action="store_true", help="skip the question (still needs a terminal)")
    sp = add(sub, "status", cmd_status, "Where things stand.")
    sp.add_argument("--json", action="store_true")
    sp = add(sub, "next", cmd_next, "The one thing to do now, with instructions.")
    sp.add_argument("--json", action="store_true")
    add(sub, "verify", cmd_verify, "Check that ledger, contract and git agree.")

    track = sub.add_parser("track", help="Code tracks: one branch and worktree each.").add_subparsers(dest="action", required=True)
    sp = add(track, "new", cmd_track_new, "Open a track.")
    sp.add_argument("name")
    sp.add_argument("--from", dest="from_ref", help="commit or branch to start from (default: the home branch)")
    sp.add_argument("--review", help="the latest closed review, which decided to open it")
    sp.add_argument("--params", help="JSON file with the track's starting params")
    sp = add(track, "set", cmd_track_set, "Park, close or reactivate a track.")
    sp.add_argument("name")
    sp.add_argument("--status", required=True, choices=["active", "parked", "closed"])
    sp.add_argument("--note", required=True)
    sp.add_argument("--review", help="the latest closed review, which decided it")
    sp = add(track, "sync", cmd_track_sync, "Record that a track branch was changed by hand.")
    sp.add_argument("name")
    sp.add_argument("--note", required=True)
    sp = add(track, "worktree", cmd_track_worktree, "Put a track's worktree back when its folder is gone.")
    sp.add_argument("name")
    add(track, "list", cmd_track_list, "List tracks.")

    hyp = sub.add_parser("hyp", help="Hypotheses: claims, separate from the experiments that test them.").add_subparsers(dest="action", required=True)
    sp = add(hyp, "new", cmd_hyp_new, "Start a hypothesis file.")
    sp.add_argument("--title", required=True)
    sp.add_argument("--from", dest="source", help="where it comes from (an experiment, an idea, a review)")
    sp = add(hyp, "set", cmd_hyp_set, "Change a hypothesis's status.")
    sp.add_argument("id")
    sp.add_argument("--status", required=True, choices=HYP_STATES)
    sp.add_argument("--note", required=True)
    sp = add(hyp, "list", cmd_hyp_list, "List hypotheses.")
    sp.add_argument("--status", choices=HYP_STATES)

    idea = sub.add_parser("idea", help="IDEAS.md: what could change the path, read at every review.").add_subparsers(dest="action", required=True)
    sp = add(idea, "add", cmd_idea_add, "Write an idea down.")
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", required=True, help="what it is, why it could matter, the cheapest test")
    sp.add_argument("--from", dest="source", help="where it came up")
    sp.add_argument("--kind", default="approach", help="approach, feature, diagnostic, evaluator, external ...")
    sp = add(idea, "set", cmd_idea_set, "Change an idea's status.")
    sp.add_argument("id")
    sp.add_argument("--status", required=True, choices=IDEA_STATES)
    sp.add_argument("--note", required=True)
    sp = add(idea, "list", cmd_idea_list, "List ideas.")
    sp.add_argument("--status", choices=IDEA_STATES)

    exp = sub.add_parser("exp", help="Experiments: design, run, judge.").add_subparsers(dest="action", required=True)
    sp = add(exp, "new", cmd_exp_new, "Draft an experiment.")
    sp.add_argument("--type", required=True, choices=rules.TYPES)
    sp.add_argument("--track", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--hyp", action="append", help="hypothesis it tests (repeatable)")
    sp.add_argument("--command", help="diagnose only: the command to run instead of the one in arl.toml")
    sp = add(exp, "commit", cmd_exp_commit, "Commit the code of a feature or diagnose experiment.")
    sp.add_argument("id")
    sp.add_argument("-m", "--message", required=True)
    sp = add(exp, "run", cmd_exp_run, "Lock the design, run, evaluate.")
    sp.add_argument("id")
    sp.add_argument("--allow-duplicate", metavar="WHY", help="repeat a known result on purpose")
    sp = add(exp, "judge", cmd_exp_judge, "Record the verdict.")
    sp.add_argument("id")
    sp.add_argument("--verdict", required=True, choices=["keep", "discard", "finding", "crash"])
    sp.add_argument("--reason", required=True)
    sp.add_argument("--hyp", action="append", metavar="ID=STATE", help="what the run did to a hypothesis (repeatable)")
    sp.add_argument("--simpler", action="store_true", help="keep a neutral result because it simplifies")
    sp = add(exp, "abandon", cmd_exp_abandon, "Drop a draft that never ran.")
    sp.add_argument("id")
    sp.add_argument("--reason", required=True)
    sp = add(exp, "show", cmd_exp_show, "One experiment: conditions, result, verdict.")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp = add(exp, "list", cmd_exp_list, "List experiments.")
    sp.add_argument("--track")
    sp.add_argument("--status")
    sp = add(exp, "log", cmd_exp_log, "The end of the latest log.")
    sp.add_argument("id")
    sp.add_argument("--eval", action="store_true", help="the evaluator's log")
    sp.add_argument("--lines", type=int, default=60)
    sp = add(exp, "reproduce", cmd_exp_reproduce, "Run a recorded experiment again from its commit and params.")
    sp.add_argument("id")
    sp.add_argument("--split", help="score on another split named in arl.toml")

    review = sub.add_parser("review", help="The periodic reconsideration of the path.").add_subparsers(dest="action", required=True)
    sp = add(review, "due", cmd_review_due, "Is a review due, and why.")
    sp.add_argument("--json", action="store_true")
    sp = add(review, "new", cmd_review_new, "Open a review with the facts filled in.")
    sp.add_argument("--trigger", help="why now, when it is not one of the counters")
    sp = add(review, "close", cmd_review_close, "Record the decision.")
    sp.add_argument("id")
    sp.add_argument("--decision", required=True, choices=DECISIONS)
    sp.add_argument("--note")
    sp = add(review, "resolve", cmd_review_resolve, "A person decides a waiting review (terminal only).")
    sp.add_argument("id")
    sp.add_argument("--decision", required=True, choices=DECISIONS)
    sp.add_argument("--note", required=True)
    return p


def _clean(args: argparse.Namespace) -> None:
    """Everything typed has to be text that can be stored; a title is one line."""
    for name, value in vars(args).items():
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                check_text(item, f"--{name.replace('_', '-')}")
    if isinstance(getattr(args, "title", None), str):
        args.title = one_line(args.title, "the title")


def _on_signal(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Asked to stop in any of these ways, a run takes its job down with it.
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, _on_signal)
    except ValueError:
        pass
    try:
        _clean(args)
        return args.func(args) or 0
    except ArlError as e:
        print(f"arl: {e}", file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("arl: interrupted", file=sys.stderr)
        return 130
