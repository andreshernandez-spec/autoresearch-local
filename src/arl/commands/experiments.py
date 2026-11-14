"""An experiment from its draft to its verdict, and looking one up afterwards.
Running one is in runs.py."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .. import contract, render, rules, runner
from ..gitops import Git
from ..ledger import Ledger
from ..record import HeadMove, Record, Verdict, dump, need
from ..util import ArlError, check_id, fmt, now, one_line, require_filled, slugify, write_json, write_text
from . import common, jobs
from .common import ARL


HYP_VERDICTS = ("supported", "refuted", "inconclusive", "open")


def cmd_exp_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    typ, hyps = args.type, [check_id("H", h) for h in (args.hyp or [])]
    with common.writing(ledger):
        lock = contract.require(ledger)
        cfg = ledger.cfg
        common.gate_new_work(ledger)
        common.env_or_refuse(ledger)  # nothing new is drafted while the environment cannot be used
        track = ledger.track(args.track)
        if track.status != "active":
            raise ArlError(f"track {args.track} is {track.status}")
        require_filled(ledger.track_path(args.track), f"the description of track {args.track}")
        flying = rules.in_flight(ledger, args.track)
        if flying:
            raise ArlError(
                f"{flying[0].id} is still in flight on track {args.track} ({flying[0].status}). "
                "One experiment at a time per track: finish it first."
            )
        path, wgit = common.worktree(ledger, track)
        common.park(ledger, track, path, wgit)
        dirty = wgit.dirty()
        if dirty:
            raise ArlError(f"{path} has uncommitted changes:\n  " + "\n  ".join(dirty[:10]))
        head = wgit.head()
        if head != track.head_commit:
            raise ArlError(
                f"branch {track.branch} moved outside arl ({track.head_commit[:10]} -> {head[:10]}).\n"
                f"Record it with '{ARL} track sync {args.track} --note \"...\"', then run a baseline."
            )
        params = ledger.track_params(track)
        if typ == "baseline" and hyps:
            raise ArlError("a baseline measures the track as it is. It tests no hypothesis: leave --hyp out.")
        for h in hyps:
            meta, _ = ledger.hypothesis(h)
            require_filled(ledger.hyp_path(h), f"hypothesis {h}")
            if meta.status == "dropped":
                raise ArlError(f"{h} was dropped")
        if typ in ("tune", "feature"):
            if not hyps:
                raise ArlError(f"a {typ} experiment tests a hypothesis: write one with 'arl hyp new' and pass --hyp")
            if rules.reference_for(ledger, track, lock["eval_digest"], common.data_files(ledger)[1]) is None:
                raise ArlError(f"track {args.track} has no reference under contract v{lock['version']} and the data as it is: run a baseline first")
        if args.command and typ != "diagnose":
            raise ArlError("--command is for diagnose experiments. Scored runs use the command in arl.toml.")

        eid, slug = ledger.next_id("E"), slugify(args.title)
        exp = ledger.exp_root / f"{eid}-{slug}"
        branch = f"exp/{eid}-{slug}" if typ in ("feature", "diagnose") else track.branch
        if exp.exists():
            raise ArlError(f"{exp} exists already: a leftover. Remove the folder.")
        leftover = branch != track.branch and ledger.git.branch_exists(branch)
        if leftover and ledger.git.head(branch) != head:
            raise ArlError(f"branch {branch} exists already with commits of its own. Pick another title.")
        rec = Record(
            id=eid,
            title=args.title,
            type=typ,
            track=args.track,
            hypotheses=hyps,
            status="draft",
            created=now(),
            base_commit=head,
            branch=branch,
            command=args.command or "",
        )
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
        if track.params_sha256 is None:  # a track from before the hash was kept: trust it once
            ledger.write_track_params(track, params)
            ledger.save_track(track)
        exp.mkdir(parents=True)
        try:
            write_text(exp / "design.md", design)
            write_json(exp / "params.json", params)
            write_json(exp / "record.json", dump(rec))
            if branch != track.branch:
                if leftover:
                    wgit.switch(branch)  # an empty branch a cut-off try left: use it
                else:
                    wgit.switch_new(branch, head)
        except BaseException:
            shutil.rmtree(exp, ignore_errors=True)
            with contextlib.suppress(ArlError):
                if wgit.branch() != track.branch and not wgit.dirty():
                    wgit.switch(track.branch)
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
    with common.writing(ledger):
        common.gate_paused(ledger)
        rec = ledger.record(args.id)
        if rec.type not in ("feature", "diagnose"):
            raise ArlError(f"{rec.id} is a {rec.type} experiment: it does not change code")
        jobs.settle(ledger, rec)
        if rec.status == "paused":
            raise ArlError(
                f"{rec.id} is paused in the middle of its run, and a run continues the code it started with.\n"
                f"Continue it as it is:  {ARL} exp run {rec.id}\n"
                f"Or give that run up, so the code can change and the next run starts from nothing:\n"
                f"  {ARL} exp fail {rec.id} --reason \"...\""
            )
        if rec.status not in ("draft", "crashed"):
            raise ArlError(f"{rec.id} is {rec.status}: its code is fixed")
        path, wgit = common.exp_worktree(ledger, rec)
        dirty = wgit.dirty()
        if not dirty:
            raise ArlError(f"nothing to commit in {path}")
        # research/ belongs to the home checkout. The only change allowed there is one
        # that puts a file back to what the track started from.
        base, wrong = rec.base_commit, []
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
        sha = wgit.commit_all(f"{rec.id}: {one_line(args.message, 'the message')}")
    print(f"{rec.id}: committed {sha[:10] if sha else ''} on {rec.branch} ({len(dirty)} paths)")
    return 0


def cmd_exp_pause(args: argparse.Namespace) -> int:
    """Ask a run that is going to save its state and stop. It continues later with 'exp run'."""
    ledger = Ledger.locate()
    asked = jobs.ask_to_pause(ledger, check_id("E", args.id) if args.id else None)
    if not asked:
        raise ArlError("nothing is running" + (f" under {args.id}" if args.id else ""))
    jobs.say_asked(ledger, asked)
    return 0


def cmd_exp_fail(args: argparse.Namespace) -> int:
    """Give up the saved state of a paused run. The attempt counts as failed."""
    ledger = Ledger.locate()
    eid = check_id("E", args.id)
    with common.writing(ledger):
        common.gate_paused(ledger)
        rec = ledger.record(eid)
        jobs.settle(ledger, rec)
        if rec.status != "paused":
            raise ArlError(f"{eid} is {rec.status}. Only a paused run is given up this way.")
        n = rec.attempts[-1].n
        jobs.fail(rec, f"not continued: {args.reason}")
        ledger.save_record(rec)
        common.finish(ledger, f"{eid} attempt {n} given up")
    left = ledger.cfg.max_attempts - len(rec.attempts)
    print(f"{eid}: attempt {n} counts as failed. Its saved state stays in artifacts/run-{n} and is not used again.")
    if left > 0:
        print(f"The next run starts from nothing ({left} attempt(s) left):  {ARL} exp run {eid}")
    else:
        print(f"That was the last attempt. Judge it:  {ARL} exp judge {eid} --verdict crash --reason \"...\"")
    return 0


def cmd_exp_judge(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    eid, verdict = check_id("E", args.id), args.verdict
    cut_off = ledger.record(eid).status == "judging"
    with common.writing(ledger):  # finishes a verdict that was cut off, this one's included
        contract.require(ledger)
        common.gate_paused(ledger)
        rec = ledger.record(eid)
        if cut_off:
            given = rec.decision
            print(f"The verdict of {eid} ({given}) was recorded before and cut off. It is complete now" + (f"; '{verdict}' was not applied." if verdict and verdict != given else "."))
            print(render.summary(ledger, rec))
            return 0
        if verdict is None or args.reason is None:
            raise ArlError("say what the verdict is and why: --verdict <keep|discard|finding|crash> --reason \"...\"")
        jobs.settle(ledger, rec)
        if rec.status not in ("evaluated", "crashed", "paused"):
            raise ArlError(f"{eid} is {rec.status}: there is nothing to judge")
        allowed = rules.allowed_verdicts(rec)
        outcome = rec.outcome
        if verdict not in allowed:
            what = outcome or ("not scored" if rec.status == "evaluated" else rec.status)
            raise ArlError(f"{eid} is '{what}': the verdict can be {' or '.join(allowed)}, not {verdict}")
        if args.simpler and not (verdict == "keep" and outcome == "neutral"):
            raise ArlError("--simpler is only for keeping a neutral result")
        if verdict == "keep" and outcome == "neutral" and not args.simpler:
            raise ArlError("a neutral result is kept only when it makes things simpler: pass --simpler and say how in --reason")
        exp = ledger.exp_dir(eid)
        if rec.status == "evaluated":
            require_filled(exp / "analysis.md", f"the analysis of {eid}")
            common.require_design_unchanged(ledger, rec)
        updates: dict[str, str] = {}
        for item in args.hyp or []:
            hid, _, state = item.partition("=")
            if hid not in rec.hypotheses or state not in HYP_VERDICTS:
                raise ArlError(f"--hyp takes <id>=<{'|'.join(HYP_VERDICTS)}> for a hypothesis of {eid}, got '{item}'")
            meta, _ = ledger.hypothesis(hid)  # it must still be there, before anything is changed
            if meta.status == "dropped" and state != "open":
                raise ArlError(f"{hid} was dropped. Pass {hid}=open, or reopen it first with 'arl hyp set'.")
            updates[hid] = state
        if verdict == "crash" and any(state != "open" for state in updates.values()):
            raise ArlError("a crash says nothing about a hypothesis: leave --hyp out, or pass <id>=open")
        if rec.status == "evaluated" and rec.type in ("tune", "feature"):
            missing = [h for h in rec.hypotheses if h not in updates]
            if missing:
                raise ArlError("say what this run did to each hypothesis: " + " ".join(f"--hyp {h}=<{'|'.join(HYP_VERDICTS)}>" for h in missing))

        track = ledger.track(rec.track)
        path, wgit = common.worktree(ledger, track)
        changed = wgit.tracked_changes()
        if changed:
            raise ArlError(
                f"tracked files in {path} differ from the commit that ran:\n  "
                + "\n  ".join(changed[:10])
                + "\nThese edits are not part of what was measured. Copy what you want to keep somewhere else,\n"
                + f"then put the files back: git -C {path} restore <file>"
            )
        if rec.branch != track.branch and wgit.branch() != rec.branch:
            raise ArlError(f"{path} is on '{wgit.branch()}', expected '{rec.branch}'")
        if verdict == "keep":
            if wgit.head() != rec.code_commit:
                raise ArlError(f"{rec.branch} moved after the run ({need(rec.code_commit)[:10]} was measured). Keep needs the measured commit.")
            common.load_params(exp / "params.json")

        # Everything is checked. Write the verdict first: from here on it is decided, and
        # the branch, the track and the hypotheses follow, now or when the next command runs.
        rec.verdict = Verdict(
            decision=verdict,
            reason=args.reason,
            at=now(),
            hypotheses=updates,
            track_head=HeadMove(before=track.head_commit),
            simpler=True if args.simpler else None,
        )
        rec.forget_job()
        rec.status = "judging"
        ledger.save_record(rec)
        common.finish_judge(ledger, rec)
    print(render.summary(ledger, rec))
    print()
    print(render.next_text(ledger, common.next_action(ledger)), end="")
    return 0


def cmd_exp_abandon(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    eid = check_id("E", args.id)
    with common.writing(ledger):
        common.gate_paused(ledger)
        rec = ledger.record(eid)
        jobs.settle(ledger, rec)
        if rec.status != "draft" or rec.attempts:
            how = f" It ran: judge it ('{ARL} exp judge {eid} --verdict crash --reason \"...\"')." if rec.status in ("crashed", "paused") else ""
            raise ArlError(f"only a draft that never ran can be abandoned. {eid} is {rec.status}.{how}")
        track = ledger.track(rec.track)
        path = ledger.worktree_path(track.name)
        # The worktree only has to leave the draft's branch. A folder that is gone has
        # nothing to leave: 'track worktree' puts it back on the track's branch.
        wgit = Git(path) if path.exists() else None
        if wgit is not None and rec.branch != track.branch and wgit.branch() == rec.branch:
            if wgit.dirty():
                raise ArlError(f"{path} has uncommitted changes. Commit them to {rec.branch} ('{ARL} exp commit {eid} -m \"...\"') or undo them first.")
            wgit.switch(track.branch)
        rec.status = "abandoned"
        rec.verdict = Verdict(decision="abandoned", reason=args.reason, at=now())
        ledger.save_record(rec)
        common.finish(ledger, f"{eid} abandoned")
    print(f"{eid} abandoned. Its branch, if any, stays in git.")
    return 0


# --- looking one up ---------------------------------------------------------------


def cmd_exp_show(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rec = ledger.record(args.id)
    if args.json:
        print(json.dumps(dump(rec), indent=2))
        return 0
    exp = ledger.exp_dir(args.id)
    print(render.summary(ledger, rec))
    print(f"  tests: {' '.join(rec.hypotheses) or 'no hypothesis'}")
    print(f"  code: {(rec.code_commit or rec.base_commit)[:10]} on {rec.branch} (track head was {rec.base_commit[:10]})")
    try:
        print(f"  params: {json.dumps(common.load_params(exp / 'params.json'))}")
    except ArlError as e:
        print(f"  params: {e}")
    for a in rec.attempts:
        took = f"run {(a.run_seconds or 0):.0f} s" + (f", evaluation {a.eval_seconds:.0f} s" if a.eval_seconds is not None else "")
        parts = len(a.segments)
        took += f", in {parts} parts" if parts > 1 else ""
        state = f", FAILED: {a.failed}" if a.failed else (f", PAUSED: {a.paused}" if a.paused else "")
        saved = f", state last saved {a.last_checkpoint[:16]}" if a.last_checkpoint else ""
        print(f"  attempt {a.n}: {took}{state}{saved}")
        if a.warning:
            print(f"    note: {a.warning}")
    name = ledger.cfg.primary.name
    for r in rec.reruns:
        if r.failed:
            print(f"  rerun {r.n} on {r.split}: FAILED: {r.failed}")
        else:
            value = fmt(r.metrics[name]) if r.metrics and name in r.metrics else "not reported"
            changed = r.env_changed
            other = f", in another environment ({runner.differences(changed.count)}: {runner.short(changed.first, 3)})" if changed else ""
            if r.data_digest not in (None, rec.data_digest):
                other += ", on other data than the experiment read"
            if r.meets_target is not None:
                other += ", meets the target" if r.meets_target else ", does not meet the target"
            print(f"  rerun {r.n} on {r.split}: {name} {value}" + (f", reproduced: {r.reproduced}" if r.reproduced is not None else "") + other)
    env = rec.env
    if env:
        what = [env.get("prefix") or "the python of the shell that ran it"] + ([f"python {env['python']}"] if env.get("python") else [])
        digest = env.get("listing") or env.get("packages")
        listing = f"; every package: {ledger.name}/envs/{digest}.txt" if digest else ""
        print(f"  environment: {', '.join(what)}{listing}")
        print(f"  ran under: {_contract_at(ledger, need(rec.contract).version)}, arl {env.get('arl', '(version not recorded)')}, on {env.get('host', 'an unnamed machine')}")
    print(f"  files: {exp}")
    code, base = rec.code_commit, rec.base_commit
    if code and code != base:
        print(f"  its code change:  git -C {ledger.home} diff {base[:10]} {code[:10]} -- . ':(exclude){ledger.name}'")
    if env:
        print(f"  what changed since: {ARL} exp env {rec.id}")
    if rec.type in rules.SCORED and rec.metrics is not None:
        print(f"  run it again:     {ARL} exp reproduce {rec.id}")
        print(f"  start from it:    {ARL} track new <name> --from {rec.id}")
    return 0


def _contract_at(ledger: Ledger, version: int) -> str:
    """A contract version with the commit that froze it, so its files can be read as they were."""
    try:
        found = ledger.git("log", "-1", "--format=%h", "--grep", f"freeze contract v{version}$").strip()
    except ArlError:
        found = ""
    return f"contract v{version}" + (f" (frozen in {found}: git show {found}:{ledger.name}/arl.toml)" if found else "")


def _env_lines(ledger: Ledger, env: dict[str, Any]) -> list[str]:
    lines = [f"  environment: {env.get('prefix') or 'not named by the contract'}" + (f", python {env['python']}" if env.get("python") else "")]
    lines.append(f"  machine: {env.get('host', 'not recorded')}, {env.get('platform', 'system not recorded')}")
    if env.get("gpu"):
        lines.append(f"  GPU and driver: {'; '.join(map(str, env['gpu']))}")
    lines.append(f"  arl: {env.get('arl', 'version not recorded')}")
    for key, label in (("listing", "every package python saw"), ("packages", "the output of [env] command")):
        if env.get(key):
            text = runner.stored_listing(ledger, env, key)
            size = f"{len(text.splitlines())} lines" if text is not None else "no longer in the ledger"
            lines.append(f"  {label}: {ledger.name}/envs/{env[key]}.txt ({size})")
    return lines


def cmd_exp_env(args: argparse.Namespace) -> int:
    """The environment an experiment ran in, and what differs from now or from another experiment."""
    ledger = Ledger.locate()
    rec = ledger.record(check_id("E", args.id))
    env = rec.env
    if not env:
        raise ArlError(f"{rec.id} has not run: there is no environment to show")
    print(f"{rec.id} ran in:")
    print("\n".join(_env_lines(ledger, env)))
    if args.other:
        other = ledger.record(check_id("E", args.other))
        there = other.env
        if not there:
            raise ArlError(f"{other.id} has not run: there is no environment to compare with")
        differs, against = runner.env_differences(ledger, env, there), f"the one {other.id} ran in"
    else:
        probed = common.env_or_problem(ledger)
        if probed and probed.get("problem"):
            print(f"The environment cannot be asked now: {probed['problem'].splitlines()[0]}")
            return 1
        now_env, texts = runner.snapshot(ledger, probed, store=False)
        differs, against = runner.env_differences(ledger, env, now_env, texts), "the environment as it is now"
    if differs:
        print(f"Compared with {against}, {runner.differences(len(differs))}:")
        print("\n".join(f"  {line}" for line in differs))
    else:
        print(f"Compared with {against}: no difference in what was recorded.")
    return 0


def cmd_exp_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    exps = [
        e
        for e in ledger.experiments()
        if (not args.track or e.track == args.track) and (not args.status or e.status == args.status)
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
