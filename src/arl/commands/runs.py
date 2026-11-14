"""Running an experiment, and running a recorded one again."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import shutil
import time
from typing import Any

from .. import contract, render, rules, runner
from ..gitops import Git
from ..ledger import Ledger
from ..util import EXIT_CRASH, EXIT_PAUSED, ArlError, canonical, check_id, fmt, now, require_filled, sha256_text, write_text
from . import common, jobs
from .common import ARL


def cmd_exp_run(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    eid = check_id("E", args.id)
    # The run lock is held from the checks to the end: no second run can start in
    # between, and whatever a record still calls "running" is a leftover.
    with runner.run_lock(ledger):
        with common.writing(ledger):
            lock = contract.require(ledger)
            jobs.settle_all(ledger)
            rec = ledger.record(eid)
            cfg = ledger.cfg
            if rec["status"] not in ("draft", "crashed", "paused"):
                raise ArlError(f"{eid} is {rec['status']}. To run it again use '{ARL} exp reproduce {eid}'.")
            resuming = rec["status"] == "paused"
            attempts = rec["attempts"]
            if not resuming and len(attempts) >= cfg.max_attempts:
                raise ArlError(
                    f"{eid} has failed {len(attempts)} times, the limit. Judge it:\n"
                    f"  {ARL} exp judge {eid} --verdict crash --reason \"...\""
                )
            common.gate_running(ledger, first_attempt=not attempts)
            exp = ledger.exp_dir(eid)
            require_filled(exp / "design.md", f"the design of {eid}")
            params = common.load_params(exp / "params.json")
            for h in rec["hypotheses"]:
                require_filled(ledger.hyp_path(h), f"hypothesis {h}")
            track = ledger.track(rec["track"])
            path, wgit = common.exp_worktree(ledger, rec)
            typ = rec["type"]
            dirty = wgit.dirty()
            if dirty:
                how = f"Commit them with '{ARL} exp commit {eid} -m \"...\"' or undo them." if typ in ("feature", "diagnose") and not resuming else "Undo them."
                raise ArlError(
                    f"{path} has uncommitted changes, so the run would not be the committed code:\n  "
                    + "\n  ".join(dirty[:10])
                    + f"\n{how}"
                )
            code, base = wgit.head(), rec["base_commit"]
            give_up = f"Judge it as a crash ('{ARL} exp judge {eid} --verdict crash --reason \"...\"') and draft it again."
            again = give_up if attempts else f"Abandon it ('{ARL} exp abandon {eid} --reason \"...\"') and draft again."
            fingerprint, data = common.data_files(ledger)
            probed = common.env_or_refuse(ledger)
            if resuming:
                if code != rec["code_commit"]:
                    raise ArlError(f"{path} is at {code[:10]}, but {eid} was paused while running {rec['code_commit'][:10]}. A run continues the code it started with. {give_up}")
                if lock["digest"] != rec["contract"]["digest"]:
                    raise ArlError(f"the contract was frozen anew since {eid} started (v{rec['contract']['version']} to v{lock['version']}), so it cannot be continued. {give_up}")
                if rec["conditions"].get("data_digest") not in (None, data):
                    raise ArlError(f"the data files changed since {eid} started, so it cannot be continued. {give_up}")
            elif typ in ("baseline", "tune"):
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
            design_hash, params_hash = common.design_hashes(exp)
            if "design" in rec:
                common.require_design_unchanged(ledger, rec)
            scored = typ in rules.SCORED
            split = cfg.splits[0]
            command = rec.get("command") or cfg.run_command
            # The files, not the commit: the same change committed twice is the same experiment.
            what = {"track": rec["track"], "tree": wgit.tree(code), "params": params_hash, "command": command, "eval": lock["eval_digest"] if scored else "", "split": split, "data": data}
            key = sha256_text(canonical(what))
            reference = rules.reference_for(ledger, track, lock["eval_digest"], data)
            repeat = typ == "baseline" and reference is not None  # the track has a reference: this one shows the noise
            if not resuming:
                for other in ledger.experiments():
                    same = other["id"] != eid and other.get("dedup") == key and other["status"] in ("evaluated", "judging", "judged")
                    if same and other.get("verdict", {}).get("decision") != "crash":
                        if not args.allow_duplicate:
                            raise ArlError(
                                f"{eid} has the same code, params, data and evaluator as {other['id']}: the result is already known.\n"
                                "To repeat it on purpose (to see the noise, or after the environment changed), pass --allow-duplicate \"why\"."
                            )
                        rec["duplicate_of"], rec["duplicate_reason"] = other["id"], args.allow_duplicate
            if typ in ("tune", "feature") and reference is None:
                raise ArlError(f"track {rec['track']} has no reference under contract v{lock['version']} and the data as it is. {again} Then run a baseline first.")
            jobs.require_free_machine(cfg)

            # All checks passed. Mark it as running and lock the design; if that
            # cannot be committed, nothing ran and nothing is counted.
            before = json.dumps(rec)
            try:
                if resuming:
                    attempt = attempts[-1]
                    attempt.pop("paused", None)
                else:
                    attempt = {"n": len(attempts) + 1, "started": now(), "code_commit": code, "segments": []}
                    attempts.append(attempt)
                    rec["contract"] = {k: lock[k] for k in ("version", "digest", "eval_digest")}
                    rec["conditions"] = {"command": command, "split": split, "worktree": str(path), "data": fingerprint, "data_digest": data}
                n = attempt["n"]
                part = {"started": now()}
                attempt.setdefault("segments", []).append(part)
                # A pause is asked of a record that says "running": clear an old request before it does.
                (exp / "logs" / "pause.request").unlink(missing_ok=True)
                rec.update(status="running", pid=os.getpid(), pid_start=runner.proc_start(os.getpid()), code_commit=code, dedup=key)
                rec.setdefault("design", {"design_sha256": design_hash, "params_sha256": params_hash, "locked_at": now()})
                part["env"] = runner.environment(ledger, probed)
                rec["conditions"].setdefault("env", part["env"])
                ledger.save_record(rec)
                parts = len(attempt["segments"])
                common.finish(ledger, f"{eid} continued, part {parts}" if parts > 1 else (f"{eid} design, {typ} on {rec['track']}" if n == 1 else f"{eid} attempt {n}"))
            except BaseException:
                ledger.save_record(json.loads(before))
                raise

        out, log, eval_log = jobs.places(exp, "run", n)
        out.mkdir(parents=True, exist_ok=True)
        values = {"params": exp / "params.json", "out": out, "split": split, "home": ledger.home, "research": ledger.dir, "exp": exp, "worktree": path, "metrics": exp / "metrics.json"}

        def started(pid: int) -> None:
            # No other command writes a record that says "running": each of them refuses it.
            # So the copy held here is the record, until the outcome is written.
            rec["child"] = runner.child_ref(pid)  # so a later arl can stop the job if this one is killed
            ledger.save_record(rec)

        def run_ended(step: jobs.Step) -> None:
            part.update(ended=now(), ended_by=step.ended_by, seconds=round(part.get("seconds", 0) + step.seconds, 2))
            attempt["run_seconds"] = round(sum(s.get("seconds", 0) for s in attempt["segments"]), 2)
            if step.exit_code is None:
                return  # it was not started, or arl was stopped while it was going
            attempt["exit_code"] = step.exit_code
            if step.ended_by == "finished":
                attempt["run_done"] = True
            write_text(exp / "run.tail.txt", runner.tail(log))
            saved = runner.last_checkpoint(out)
            if saved:
                attempt["last_checkpoint"] = dt.datetime.fromtimestamp(saved).astimezone().isoformat(timespec="seconds")
            elif cfg.checkpoint_every and step.seconds > 3 * cfg.checkpoint_every:
                attempt["warning"] = f"this part ran {step.seconds:.0f} s and saved no state under {{out}}/checkpoint: a pause loses that work"

        def evaluation_ended(step: jobs.Step) -> None:
            attempt["eval_seconds"] = round(attempt.get("eval_seconds", 0) + step.seconds, 2)
            if step.exit_code is not None:
                write_text(exp / "eval.tail.txt", runner.tail(eval_log))

        job = jobs.Job(
            what="run",
            command=command,
            cwd=path,
            values=values,
            env=jobs.run_env(ledger, rec, values, resume=len(attempt["segments"]) > 1),
            log=log,
            eval_log=eval_log,
            contract=rec["contract"]["digest"] if scored else None,
            data=rec["conditions"].get("data_digest"),
            timeout=cfg.run_timeout,
            used=sum(s.get("seconds", 0) for s in attempt["segments"][:-1]),
            on_start=started,
            run_ended=run_ended,
            evaluation_ended=evaluation_ended,
            same_code=lambda: wgit.branch() == rec["branch"] and wgit.head() == code and not wgit.tracked_changes(),
        )
        try:
            got = jobs.measure(ledger, job, run=not attempt.get("run_done"))
            if got.artifacts is not None:
                rec["artifacts"] = got.artifacts
            if got.train_metrics:
                rec["train_metrics"] = got.train_metrics  # what the run says about itself: kept, never compared
            if got.note:
                attempt["warning"] = "; ".join(filter(None, [attempt.get("warning"), got.note]))
            if got.metrics is not None:
                rec["metrics"] = got.metrics
                rec["comparison"] = rules.compare(cfg, got.metrics, reference, repeat=repeat)
        except BaseException:
            # arl itself is being stopped (a closed terminal, Ctrl-C). The job was given the
            # time to save its state. This is an interruption, not a failure.
            jobs.pause_run(rec, "interrupted: arl was stopped while the run was going")
            ledger.save_record(rec)
            with contextlib.suppress(ArlError):
                with ledger.locked():
                    common.finish(ledger, f"{eid} interrupted")
            raise
        failed, paused = got.failed, got.paused

        def write_outcome() -> None:
            if rec["status"] != "running":
                return  # written already, and it could not be committed then
            attempt["ended"] = now()
            for key in ("pid", "pid_start", "child"):
                rec.pop(key, None)
            if failed:
                jobs.fail(rec, failed)
                ledger.save_record(rec)
                return
            if paused:
                attempt.pop("ended")
                jobs.pause_run(rec, paused)
                ledger.save_record(rec)
                return
            rec["status"] = "evaluated"
            rec["kept"] = runner.keep_small(out, exp / "kept", cfg.keep, cfg.keep_max_kb)
            ledger.save_record(rec)  # the result is safe before anything else is written about it
            kind = "scored" if scored else "diagnose"
            try:
                facts = render.analysis_facts(ledger, rec)
            except (ArlError, KeyError) as e:
                facts = f"Written by arl. The table of results could not be written ({e}). The numbers are in metrics.json and record.json."
            write_text(exp / "analysis.md", render.fill(render.template(f"analysis_{kind}.md"), id=eid, facts=facts))

        if failed:
            message = f"{eid} failed, attempt {n}"
        elif paused:
            message = f"{eid} paused"
        else:
            message = f"{eid} result" + (f", {cfg.primary.name} {fmt(rec['metrics'][cfg.primary.name])}" if scored else "")
        jobs.keep_outcome(ledger, eid, write_outcome, message)

    if failed:
        print(f"{eid} FAILED (attempt {n} of {cfg.max_attempts}): {failed}")
        print(f"Log: {ARL} exp log {eid}")
        return EXIT_CRASH
    if paused:
        print(f"{eid} PAUSED after {attempt['run_seconds']:.0f} s of running: {paused}")
        print(f"It continues from the state it last saved:  {ARL} exp run {eid}")
        return EXIT_PAUSED
    print(render.summary(ledger, rec))
    if attempt.get("warning"):
        print(f"  note: {attempt['warning']}")
    print()
    print(render.next_text(ledger, {"stage": "judge", "target": eid, "why": f"{eid} has a result and no verdict"}), end="")
    return 0


def cmd_exp_reproduce(args: argparse.Namespace) -> int:
    """Run a recorded experiment again from its commit and params, on any split."""
    ledger = Ledger.locate()
    eid = check_id("E", args.id)
    with runner.run_lock(ledger):
        with common.writing(ledger):
            lock = contract.require(ledger)
            jobs.settle_all(ledger)
            rec = ledger.record(eid)
            cfg = ledger.cfg
            if rec["type"] not in rules.SCORED or "metrics" not in rec:
                raise ArlError("only a scored experiment with a result can be run again")
            common.gate_running(ledger, first_attempt=False)
            common.require_design_unchanged(ledger, rec)
            probed = common.env_or_refuse(ledger)
            split = args.split or cfg.splits[0]
            if split not in cfg.splits:
                raise ArlError(f"arl.toml knows the splits {cfg.splits}, not '{split}'")
            fingerprint, data = common.data_files(ledger)  # a rerun reads the data too: a file that is gone stops it here
            jobs.require_free_machine(cfg)
            exp = ledger.exp_dir(eid)
            n = max([r["n"] for r in rec.get("reruns", [])], default=0) + 1  # failed reruns keep their number
            tree = ledger.worktree_root() / f"{cfg.name}-rerun-{eid}-{n}"
            out, log, eval_log = jobs.places(exp, "rerun", n, split)
            if out.exists():
                shutil.rmtree(out)  # nothing recorded owns it: a rerun starts in an empty folder
            if tree.exists():
                jobs.drop_tree(ledger, tree)
            (log.parent / "pause.request").unlink(missing_ok=True)  # an old request is not meant for this rerun
            env_now = runner.environment(ledger, probed)
            rec["rerun"] = {
                "n": n,
                "split": split,
                "started": now(),
                "pid": os.getpid(),
                "pid_start": runner.proc_start(os.getpid()),
                "tree": str(tree),
                "eval_digest": lock["eval_digest"],
                "data_digest": data,
            }
            ledger.save_record(rec)

        # From here on the record belongs to whoever writes it next: a verdict may be given
        # while the rerun is going. 'rec' is read, never written back; see jobs.amend.
        out.mkdir(parents=True)
        values = {"params": exp / "params.json", "out": out, "split": split, "home": ledger.home, "research": ledger.dir, "exp": exp, "worktree": tree, "metrics": out / "metrics.json"}
        entry: dict[str, Any] = {"n": n, "at": now(), "split": split, "eval_digest": lock["eval_digest"], "data_digest": data, "env": env_now}
        same_data = rec["conditions"].get("data_digest") in (None, data)
        if not same_data:
            entry["data"] = fingerprint  # what it read instead
        # A rerun does not put the old environment back. It says what is different now,
        # since that can explain a different result. Both package lists are in the ledger.
        differs = runner.env_differences(ledger, rec["conditions"].get("env"), env_now)
        if differs:
            entry["env_changed"] = {"count": len(differs), "first": differs[:20]}

        def started(pid: int) -> None:
            ref = runner.child_ref(pid)  # so a later arl can stop the job if this one is killed

            def note(now_rec: dict[str, Any]) -> None:
                if now_rec.get("rerun", {}).get("n") == n:
                    now_rec["rerun"]["child"] = ref

            jobs.amend(ledger, eid, note)

        def close(now_rec: dict[str, Any]) -> None:
            """Put the rerun into the record as it is now. Once: a second call finds it done."""
            if now_rec.get("rerun", {}).get("n") == n:
                now_rec.setdefault("reruns", []).append(entry)
                now_rec.pop("rerun")

        wtree = Git(tree)
        job = jobs.Job(
            what="rerun",
            command=rec.get("command") or cfg.run_command,
            cwd=tree,
            values=values,
            env=jobs.run_env(ledger, rec, values, resume=False),
            log=log,
            eval_log=eval_log,
            contract=lock["digest"],
            data=data,
            timeout=cfg.run_timeout,
            on_start=started,
            run_ended=lambda step: entry.update(run_seconds=step.seconds),
            evaluation_ended=lambda step: entry.update(eval_seconds=step.seconds),
            same_code=lambda: wtree.head() == rec["code_commit"] and not wtree.tracked_changes(),
        )
        clock, failed, metrics = time.monotonic(), "", {}
        try:
            tree.parent.mkdir(parents=True, exist_ok=True)
            ledger.git.prune_worktrees()
            ledger.git.add_detached(tree, rec["code_commit"])
            got = jobs.measure(ledger, job)
            failed, metrics = got.failed, got.metrics or {}
            if got.paused:  # a rerun is not continued, so a stop is the end of it
                failed = f"the rerun was stopped: {got.paused}. A rerun is not continued: run it again"
                entry["interrupted"] = True
            if got.train_metrics and not failed:
                entry["train_metrics"] = got.train_metrics
        except ArlError as e:
            failed = str(e)
        except BaseException:
            entry.setdefault("run_seconds", round(time.monotonic() - clock, 2))
            entry.update(failed="interrupted", interrupted=True)
            with contextlib.suppress(ArlError):
                jobs.amend(ledger, eid, close, wait=20)  # if the lock is taken, the next command finds the rerun and settles it
            with contextlib.suppress(ArlError):
                jobs.drop_tree(ledger, tree)
            raise
        finally:
            if tree.exists():
                with contextlib.suppress(ArlError):
                    jobs.drop_tree(ledger, tree)
        entry.setdefault("run_seconds", round(time.monotonic() - clock, 2))
        primary = cfg.primary
        same_eval = rec["contract"]["eval_digest"] == lock["eval_digest"]
        if failed:
            entry["failed"] = failed = failed.splitlines()[0]
        else:
            entry["metrics"] = metrics
            if split == cfg.splits[0] and same_eval and same_data:
                diff = abs(metrics[primary.name] - rec["metrics"][primary.name])
                entry["difference"] = diff
                entry["reproduced"] = bool(diff <= max(primary.min_delta, 1e-12))
            if primary.target is not None:
                entry["meets_target"] = rules.meets_target(cfg, metrics)

        def write() -> None:
            now_rec = ledger.record(eid)
            close(now_rec)
            ledger.save_record(now_rec)

        jobs.keep_outcome(ledger, eid, write, f"{eid} rerun on {split}" + (" failed" if failed else ""), what="rerun")
    if failed:
        print(f"{eid} rerun on split '{split}' FAILED: {failed}. Log: {log}")
        return EXIT_CRASH
    print(f"{eid} rerun on split '{split}': {primary.name} = {fmt(metrics[primary.name])}")
    if differs:
        print(f"  the environment is not the one it first ran in ({runner.differences(len(differs))}): {runner.short(differs)}")
        print(f"  all of them: {ARL} exp env {eid}")
    else:
        print("  the environment is the one it first ran in, as far as it was recorded")
    if not same_data:
        print("  the data files are not the ones it first ran on: this is the recorded code on other data, not a repetition of the result")
    if "reproduced" in entry:
        print(f"  recorded {fmt(rec['metrics'][primary.name])}, difference {entry['difference']:.3g}, reproduced: {entry['reproduced']}")
    else:
        print("  not compared with the recorded result (another split, another evaluator or other data). Never used for a verdict.")
    hit = rules.claim(ledger, ledger.record(eid), lock) if same_data else None
    if hit and split in hit["splits"]:
        goal = fmt(float(primary.target))
        if entry["meets_target"] and hit["state"] == "missed":
            print(f"  it meets the target of {goal} here, and rerun {hit['rerun']} on '{hit['split']}' did not. That run settled it: the target counts as not reached by {eid}.")
        elif entry["meets_target"]:
            print(f"  it meets the target of {goal} as well" + (": the target is confirmed." if hit["state"] == "confirmed" else "."))
        else:
            print(f"  it does not meet the target of {goal} here: the target counts as not reached by {eid}.")
            print(f"  {eid} stays the reference of its track. Another run on '{split}' does not change this answer.")
        print()
        print(render.next_text(ledger, common.next_action(ledger)), end="")
    return 0
