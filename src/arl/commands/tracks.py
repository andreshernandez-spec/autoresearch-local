"""Code tracks: one branch and one worktree each."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from .. import contract, render, rules
from ..ledger import Ledger
from ..util import ArlError, now, write_front
from . import common


def cmd_track_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    name = args.name
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", name):
        raise ArlError("a track name is lower case letters, digits and dashes")
    with common.writing(ledger):
        contract.require(ledger)
        common.gate_paused(ledger)
        if ledger.track_path(name).exists():
            raise ArlError(f"track {name} already exists")
        opened_by = common.authorized(ledger, args.introspection, ("branch", "pivot"), "a new track")
        git, branch = ledger.git, f"track/{name}"
        source = None
        if args.from_ref and re.fullmatch(r"E\d{4}", args.from_ref):
            # An experiment: the track starts at the code it ran and, unless told otherwise, its params.
            source = ledger.record(args.from_ref)
            if source["type"] not in rules.SCORED:
                raise ArlError(f"{source['id']} is a diagnose experiment: its code observes, it is not a method. Name a commit if you mean it.")
            if not source.get("code_commit") or "design" not in source:
                raise ArlError(f"{source['id']} never ran, so there is no code to start from")
            common.require_design_unchanged(ledger, source)
            start = git.head(source["code_commit"])
            params = common.load_params(Path(args.params)) if args.params else common.load_params(ledger.exp_dir(source["id"]) / "params.json")
        else:
            start = git.head(args.from_ref or "HEAD")
            params = common.load_params(Path(args.params)) if args.params else {}
        path = ledger.worktree_path(name)
        # A branch or a worktree without a track file is what an earlier try left when it was cut off.
        if git.branch_exists(branch) and git.head(branch) != start:
            raise ArlError(f"branch {branch} exists already and is not at {start[:10]}. Pick another name.")
        registered = {w["path"]: w.get("branch") for w in git.worktrees()}
        if path.exists() and registered.get(str(path)) != branch:
            raise ArlError(f"{path} exists already and is not a worktree of {branch}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not git.branch_exists(branch):
            git.create_branch(branch, start)
        if not path.exists():
            git.prune_worktrees()
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
        common.finish(ledger, f"open track {name}")
    print(f"Track {name}: branch {branch} at {start[:10]}, worktree {path}")
    if source:
        print(f"It starts from {source['id']}: the code it ran and {'the params given' if args.params else 'its params'}. Its baseline should give the result of {source['id']} again.")
    print(f"Now write {ledger.track_path(name)} (approach, premises, stop if), below the +++ block.")
    return 0


def cmd_track_set(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with common.writing(ledger):
        contract.require(ledger)
        common.gate_paused(ledger)
        track = ledger.track(args.name)
        if track["status"] == args.status:
            raise ArlError(f"track {args.name} is already {args.status}")
        flying = rules.in_flight(ledger, args.name)
        if args.status != "active" and flying:
            raise ArlError(f"{flying[0]['id']} is still in flight on track {args.name}")
        by = common.authorized(ledger, args.introspection, (), "parking, closing or reopening a track")
        track["status"] = args.status
        common.track_log(ledger, track, f"{args.status} (by {by}). {args.note}")
        common.finish(ledger, f"track {args.name} {args.status}")
    print(f"Track {args.name} is {args.status}.")
    return 0


def cmd_track_sync(args: argparse.Namespace) -> int:
    """The branch was changed by hand. Record the new head; the track needs a new baseline."""
    ledger = Ledger.locate()
    with common.writing(ledger):
        contract.require(ledger)
        common.gate_paused(ledger)
        track = ledger.track(args.name)
        if rules.in_flight(ledger, args.name):
            raise ArlError(f"an experiment is in flight on track {args.name}")
        head = ledger.git.head(track["branch"])
        if head == track["head_commit"]:
            raise ArlError("the branch did not move")
        old = track["head_commit"]
        track["head_commit"], track["head_experiment"] = head, ""
        common.track_log(ledger, track, f"branch moved outside arl from {old[:10]} to {head[:10]}. {args.note}")
        common.finish(ledger, f"track {args.name} synced to {head[:10]}")
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
        flying = [e for e in rules.in_flight(ledger, args.name) if e["status"] != "judging"]
        branch = flying[0]["branch"] if flying else track["branch"]
        git = ledger.git
        # A clone of the project brings the commits and not the local branches. The ledger
        # says where each one stood; an experiment's own branch may be further on where
        # the project was cloned from.
        wanted = {track["branch"]: track["head_commit"]}
        if flying and branch != track["branch"]:
            there = f"refs/remotes/origin/{branch}"
            wanted[branch] = git.head(there) if git.ok("rev-parse", "--verify", "--quiet", there) else flying[0].get("code_commit") or flying[0]["base_commit"]
        missing = {name: commit for name, commit in wanted.items() if not git.branch_exists(name)}
        for name, commit in missing.items():
            if not git.commit_exists(commit):
                raise ArlError(f"branch {name} is not in this repository, and neither is the commit it stood at ({commit[:10]}). Fetch it from where the project came from.")
        for name, commit in missing.items():
            git.create_branch(name, commit)
        git.prune_worktrees()  # git still lists the folder that is gone
        path.parent.mkdir(parents=True, exist_ok=True)
        git.add_worktree(path, branch)
    for name, commit in missing.items():
        print(f"Branch {name} was not in this repository: made again at {commit[:10]}, where the ledger says it stood.")
    print(f"Track {args.name}: worktree {path} is back, on {branch}.")
    return 0


def cmd_track_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    print(render.track_table(ledger, contract.load(ledger)), end="")
    for t in ledger.tracks():
        print(f"{t['name']}: {ledger.worktree_path(t['name'])}")
    return 0
