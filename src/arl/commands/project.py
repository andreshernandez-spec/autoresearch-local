"""The project as a whole: setting it up, freezing the contract, where things stand,
what comes next, and whether ledger, contract and git still agree."""

from __future__ import annotations

import argparse
import json

from .. import contract, render, rules, runner
from ..ledger import Ledger
from ..record import need
from ..util import ArlError, as_text, read_json, write_text
from . import common
from .common import ARL


def cmd_init(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    if ledger.exists():
        raise ArlError(f"{ledger.config_path} already exists")
    for sub in ("eval", "hypotheses", "tracks", "experiments", "introspections"):
        (ledger.dir / sub).mkdir(parents=True, exist_ok=True)
    branch = ledger.git.branch() or "main"
    files = {
        ledger.program_path: render.template("PROGRAM.md"),
        ledger.knowledge_path: render.template("KNOWLEDGE.md"),
        ledger.lessons_path: render.template("LESSONS.md"),
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
            write_text(path, text)
    print(f"Created {ledger.dir}" + (f" (left as they were: {', '.join(kept)})" if kept else ""))
    print("Next: write PROGRAM.md and arl.toml, put the evaluator under eval/, say what is already")
    print("known about the problem in KNOWLEDGE.md, then")
    print(f"  {ARL} freeze --reason \"initial\"")
    return 0


def cmd_freeze(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    person = common.interactive()
    old = contract.load(ledger) if ledger.exists() else None
    shown = None
    if old and person and not args.yes:
        # Ask before taking the ledger lock, so a run that ends meanwhile can write.
        # The answer covers this list only: it is compared again under the lock.
        shown = contract.problems(ledger)
        print("Changes since contract v%d:" % old["version"])
        for line in shown:
            print(f"  {line}")
        if input(f"Freeze these as contract v{old['version'] + 1}? [y/N] ").strip().lower() not in ("y", "yes"):
            raise ArlError("not frozen")
    with ledger.locked():
        before = contract.load(ledger)
        try:
            runner.probe_env(ledger)  # a contract that names an environment nothing can run in is not frozen
        except ArlError as e:
            raise ArlError(f"{e}\nNothing was frozen. Name an environment that is on this machine.") from e
        lock = contract.freeze(ledger, args.reason, person, shown)
        try:
            write_text(ledger.status_path, render.status(ledger))
            ledger.git.commit_paths(f"{ledger.cfg.commit_prefix}: freeze contract v{lock['version']}", contract.commit_paths(ledger))
        except BaseException:
            contract.unfreeze(ledger, before)  # a freeze that is not in git did not happen
            raise
    print(f"Contract v{lock['version']} frozen ({len(lock['files'])} files).")
    if old and old["eval_digest"] != lock["eval_digest"]:
        print("What a score depends on changed (the evaluator, the metrics, the commands or the environment): every track needs a new baseline.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    if args.json:
        print(json.dumps(render.state(ledger), indent=2, default=as_text))
    else:
        print(render.status(ledger), end="")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    data = env = None
    if ledger.exists():
        ledger.check_files()  # a damaged record is the first thing to say
        data, env = runner.data_now(ledger), common.env_or_problem(ledger)
    action = rules.next_action(ledger, data, env)
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
    if not bad:
        left = contract.uncommitted(ledger)
        if left:
            bad.append("the frozen contract is not committed: " + ", ".join(left[:6]) + ". A person runs 'arl freeze' again, or commits these files.")
        env = common.env_or_problem(ledger)
        if env and env.get("problem"):
            bad.append("the environment: " + env["problem"].splitlines()[0])
    git = ledger.git
    for t in ledger.tracks():
        if not git.branch_exists(t.branch):
            bad.append(f"track {t.name}: branch {t.branch} is gone ('arl track worktree {t.name}' makes it again where the ledger says it stood)")
        elif t.status == "active" and git.head(t.branch) != t.head_commit:
            bad.append(f"track {t.name}: branch moved outside arl (run 'arl track sync {t.name}')")
        try:
            ledger.track_params(t)
        except ArlError as e:
            bad.append(f"track {t.name}: {str(e).splitlines()[0]}")
        if t.head_experiment:
            try:
                head = ledger.record(t.head_experiment)
                if not rules.kept(head) and head.status != "judging":  # a verdict cut off is named below
                    bad.append(f"track {t.name}: its reference {t.head_experiment} was not kept")
            except ArlError as e:
                bad.append(f"track {t.name}: {str(e).splitlines()[0]}")
    for folder in sorted(ledger.exp_root.glob("E[0-9][0-9][0-9][0-9]*")) if ledger.exp_root.exists() else []:
        if not (folder / "record.json").exists():
            bad.append(f"{ledger.rel(folder)} has no record.json: a leftover, not an experiment")
    for e in ledger.experiments():
        exp = ledger.exp_dir(e.id)
        if e.status == "judging":
            bad.append(f"{e.id}: its verdict ({e.decision}) was cut off before it was complete. '{ARL} exp judge {e.id}' finishes it.")
        if e.design is not None:
            try:
                design_hash, params_hash = common.design_hashes(exp)
                if design_hash != e.design.design_sha256:
                    bad.append(f"{e.id}: design.md changed after the run started")
                if params_hash != e.design.params_sha256:
                    bad.append(f"{e.id}: params.json changed after the run started")
            except ArlError as err:
                bad.append(f"{e.id}: {str(err).splitlines()[0]}")
        if e.code_commit and not git.commit_exists(e.code_commit):
            bad.append(f"{e.id}: commit {e.code_commit[:10]} is not in the repository")
        if rules.kept(e) and e.type == "feature":
            track = ledger.track(e.track)
            if git.branch_exists(track.branch) and not git.is_ancestor(need(e.code_commit), track.branch):
                bad.append(f"{e.id}: kept, but its commit is not in {track.branch}")
        if e.metrics is not None:
            try:
                stored = read_json(exp / "metrics.json") if (exp / "metrics.json").exists() else {}
            except (ArlError, json.JSONDecodeError):
                stored = {}
            if any(stored.get(k) != v for k, v in e.metrics.items()):
                bad.append(f"{e.id}: metrics.json differs from the record")
    bad += ledger.idea_problems()
    if bad:
        print("\n".join(bad))
        return 1
    print("ok")
    return 0
