"""A walk through the loop the way an agent takes it.

Mostly it does what `arl next` says. Sometimes it types another command, and
sometimes the ground is pulled away: a run is cut off, the loop is paused, a
contract file is edited, a worktree folder disappears, the environment the
contract names is moved away, a package in it changes version. In every second
walk the contract has a target that a few results reach, on one split or on
both. A person steps in only where `next` says STOP. After every step the same
things must hold: no traceback, `next` and `status` answer, the records agree
with each other, and the step that `next` asks for is not refused for ever.

More seeds or longer walks: ARL_WALK_SEEDS=40 ARL_WALK_STEPS=150 python -m pytest tests/test_walk.py
On several machines at once, ARL_WALK_SHARD=2/4 is the second of four: every fourth walk, from the second on.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import io
import json
import os
import random
import shutil

import pytest

from arl import cli, rules
from arl.commands import common
from arl.config import DECISIONS
from arl.ledger import Ledger
from conftest import NOTHING_NEW, Project, cut_off, env_table, git, install, make_env, pause_record

SEEDS = range(int(os.environ.get("ARL_WALK_SEEDS", "6")))
if os.environ.get("ARL_WALK_SHARD"):  # "2/4": the second of four machines that share the walks out
    _part, _of = map(int, os.environ["ARL_WALK_SHARD"].split("/"))
    assert 1 <= _part <= _of, "ARL_WALK_SHARD is <which machine>/<how many>, counted from 1"
    SEEDS = SEEDS[_part - 1 :: _of]
STEPS = int(os.environ.get("ARL_WALK_STEPS", "70"))
STAGES = {
    "track", "baseline", "hypothesize", "design", "outdated", "judge", "crashed", "interrupted", "resume",
    "unfinished", "introspect", "reload", "wait", "stopped", "paused", "budget", "confirm", "target", "contract", "environment",
}  # fmt: skip
STATUSES = {"draft", "running", "paused", "evaluated", "judging", "judged", "crashed", "abandoned"}
ACCEPTED = (0, 2, 3)  # done, the run failed, the run was paused: each one moved the loop on


class DeadEnd(AssertionError):
    pass


class Walk:
    def __init__(self, p: Project, seed: int, monkeypatch, env):
        self.p, self.rng, self.mp = p, random.Random(seed), monkeypatch
        self.env, self.away = env, env.with_name("env.away")  # the environment, and where it is when it is gone
        self.log: list[str] = []
        self.damaged: tuple | None = None  # a contract file edited by hand, and what it held
        self.seen: collections.Counter = collections.Counter()  # stages followed and commands by exit code
        self.n = 0
        self.person(False)

    # --- plumbing -----------------------------------------------------------------

    def person(self, there: bool) -> None:
        self.mp.setattr(common, "interactive", lambda: there)

    def call(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit:
                code = 64  # the parser refused the arguments
        text = out.getvalue()
        self.seen[f"exit {code}"] += 1
        self.log.append(f"$ arl {' '.join(map(str, argv))}   -> {code}\n" + "".join(f"    {line}\n" for line in text.splitlines()[:12]))
        assert "Traceback" not in text and code in (*ACCEPTED, 1, 64, 75, 130), self.story()
        return code, text

    def story(self, last: int = 40) -> str:
        return "\n".join(self.log[-last:])

    def name(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    def next(self) -> dict:
        code, text = self.call("next", "--json")
        assert code == 0, self.story()
        action = json.loads(text)
        assert action["stage"] in STAGES, self.story()
        return action

    def fingerprint(self) -> str:
        """Everything the loop can change: the ledger files and where the worktrees stand."""
        p, digest = self.p, hashlib.sha256()
        for path in sorted(p.research.rglob("*")):
            rel = path.relative_to(p.research).as_posix()
            skip = "/artifacts/" in rel or "/logs/" in rel or rel == ".cache/hashes.json" or path.name in ("activity.jsonl", ".arl.lock", ".run.lock")
            if path.is_file() and not skip:
                digest.update(rel.encode() + path.read_bytes())
        digest.update(git(p.root, "worktree", "list", "--porcelain").encode())
        for t in p.ledger.tracks():
            wt = p.ledger.worktree_path(t.name)
            digest.update(git(wt, "status", "--porcelain").encode() if wt.exists() else b"gone")
        return digest.hexdigest()

    def check(self) -> None:
        """What holds after every step, whatever the step was."""
        p = self.p
        assert self.call("status")[0] == 0, self.story()
        assert git(p.root, "branch", "--show-current") == "main", self.story()
        exps = p.ledger.experiments()
        for e in exps:
            assert e.status in STATUSES, self.story()
            assert (e.verdict is not None) == (e.status in ("judging", "judged", "abandoned")), f"{e.id}\n{self.story()}"
        for t in p.ledger.tracks():
            flying = [e.id for e in exps if e.track == t.name and e.status in rules.IN_FLIGHT]
            assert len(flying) <= 1, f"{flying}\n{self.story()}"
            if t.head_experiment:
                head = p.record(t.head_experiment)  # kept, or being kept by a verdict that was cut off
                assert head["verdict"]["decision"] == "keep" and head["status"] in ("judged", "judging"), self.story()

    # --- what the agent does when it follows `next` ----------------------------------

    def worktree(self, track: str):
        wt = self.p.ledger.worktree_path(track)
        if not wt.exists():
            self.call("track", "worktree", track)  # what every refusal about a missing worktree says to do
        return wt

    def judge(self, eid: str, verdict: str | None = None) -> int:
        p = self.p
        rec = p.record(eid)
        analysis = p.ledger.exp_dir(eid) / "analysis.md"
        if analysis.exists():
            p.fill(analysis)
        self.worktree(rec["track"])
        allowed = rules.allowed_verdicts(p.ledger.record(eid)) or ["crash"]
        verdict = verdict or self.rng.choice(allowed)
        args = ["exp", "judge", eid, "--verdict", verdict, "--reason", "what the walk decided"]
        if rec["status"] == "evaluated" and rec["type"] in ("tune", "feature"):
            for h in rec["hypotheses"]:
                dropped = p.ledger.hypothesis(h)[0].status == "dropped"
                args += ["--hyp", f"{h}={'open' if dropped else self.rng.choice(['supported', 'refuted', 'inconclusive', 'open'])}"]
        if verdict == "keep" and rec.get("comparison", {}).get("outcome") == "neutral":
            args.append("--simpler")
        if self.rng.random() >= 0.2:
            return self.call(*args)[0]
        # The process is stopped somewhere in the middle of applying the verdict.
        owner, step = self.rng.choice([(Ledger, "write_track_params"), (Ledger, "set_hypothesis"), (Ledger, "log_hypothesis"), (Ledger, "save_track"), (common, "finish")])
        real = getattr(owner, step)

        def stopped_here(*args, **kwargs):
            raise KeyboardInterrupt

        setattr(owner, step, stopped_here)
        try:
            code = self.call(*args)[0]
        finally:
            setattr(owner, step, real)
        if code == 130:
            self.log.append(f"# the verdict of {eid} was cut off at {step}")
        return code

    def run(self, eid: str) -> int:
        code, text = self.call("exp", "run", eid)
        if code == 1 and "already known" in text:
            code, text = self.call("exp", "run", eid, "--allow-duplicate", "to see the noise")
        return code

    def introspect(self, nid: str | None, decision: str | None = None) -> int:
        p, rng = self.p, self.rng
        if nid is None:
            code, text = self.call("introspect", "new")
            if code != 0:
                return code
            nid = text.split()[0]
        p.fill(p.research / "introspections" / f"{nid}.md")
        flags = []
        if rng.random() < 0.5:
            self.call("lesson", "add", "--scope", rng.choice(["general", "project"]), "--title", self.name("lesson "), "--body", "What happened. What to do instead.", "--from", nid)
        else:
            flags += ["--no-lessons", "nothing went wrong"]
        if rng.random() < 0.5:
            knowledge = p.research / "KNOWLEDGE.md"
            knowledge.write_text(knowledge.read_text().replace("## Likely\n", f"## Likely\n\n- {self.name('finding ')} ({nid}).\n", 1))
        else:
            flags += ["--knowledge-unchanged", "nothing new"]
        decision = decision or rng.choice(["continue"] * 4 + ["adjust", "branch", "branch", "pivot", "stop", "escalate"])
        return self.call("introspect", "close", nid, "--decision", decision, *flags)[0]

    def follow(self, action: dict) -> bool:
        """Do what the brief of this stage says. False ends the walk (a STOP nobody lifts)."""
        p, rng = self.p, self.rng
        stage, target = action["stage"], action.get("target")
        exps, tracks = p.ledger.experiments(), p.ledger.tracks()
        if stage in ("budget", "target"):
            return False
        if stage == "environment":
            assert self.away.exists(), self.story()
            self.away.rename(self.env)  # the person puts it back; the agent only reported it
        elif stage == "contract":
            assert self.damaged, self.story()
            path, text = self.damaged
            path.write_text(text)  # changed by accident: the frozen version goes back
            self.damaged = None
        elif stage == "paused":
            self.person(p.ledger.paused().get("by") == "a person at a terminal")  # lifted the way it was set
            self.call("resume")  # the person said to go on
            self.person(False)
        elif stage == "wait":
            self.person(True)
            self.call("introspect", "resolve", target, "--decision", rng.choice(["continue", "adjust", "branch", "stop"]), "--note", "decided at the terminal")
            self.person(False)
        elif stage == "stopped":
            if rng.random() < 0.3:
                return False
            self.introspect(None, "continue")  # the agent may write one, the person reopens
        elif stage == "reload":
            self.call("reload")
        elif stage == "introspect":
            self.introspect(target)
        elif stage == "track":
            state = rules.introspection_state(p.ledger)
            parked = [t.name for t in tracks if t.status == "parked"]
            if state["last"] and parked and rng.random() < 0.5:
                self.worktree(parked[0])
                self.call("track", "set", parked[0], "--status", "active", "--note", "one more try", "--introspection", state["last"])
            else:
                name = self.name("t")
                by = ["--introspection", state["last"]] if state["last"] and state["last_decision"] in ("branch", "pivot") else []
                self.person(not by)
                if self.call("track", "new", name, *by)[0] == 0:
                    p.fill(p.research / "tracks" / f"{name}.md")
                self.person(False)
        elif stage == "baseline":
            self.worktree(target)
            p.fill(p.research / "tracks" / f"{target}.md")
            self.call("exp", "new", "--type", "baseline", "--track", target, "--title", self.name("baseline "))
        elif stage == "hypothesize":
            usable = [h.id for h in p.ledger.hypotheses() if h.status != "dropped"]
            if not usable or rng.random() < 0.4:
                h = self.call("hyp", "new", "--title", self.name("claim "))[1].split(":")[0]
                p.fill(p.ledger.hyp_path(h))
            else:
                h = rng.choice(usable)
            track = rng.choice([t.name for t in tracks if t.status == "active"])
            self.worktree(track)
            typ = rng.choice(["tune"] * 5 + ["feature"] * 3 + ["diagnose", "diagnose", "baseline"])
            args = ["exp", "new", "--type", typ, "--track", track, "--title", self.name(f"{typ} ")]
            args += ["--hyp", h] if typ in ("tune", "feature") else []
            args += ["--command", "echo observed > {out}/note.txt"] if typ == "diagnose" and rng.random() < 0.7 else []
            self.call(*args)
        elif stage == "design":
            rec = p.record(target)
            exp = p.ledger.exp_dir(target)
            wt = self.worktree(rec["track"])
            p.fill(exp / "design.md")
            if rec["type"] == "tune" and not rec["attempts"]:
                params = {"x": round(rng.uniform(-0.5, 1.2), 2)}
                if rng.random() < 0.2:
                    params["crash"] = True
                (exp / "params.json").write_text(json.dumps(params))
            if rec["type"] == "feature" and not git(wt, "log", "--oneline", f"{rec['base_commit']}..HEAD"):
                p.edit_code(rec["track"], round(rng.uniform(-0.3, 0.9), 2))
                self.call("exp", "commit", target, "-m", "another offset")
            if self.run(target) == 1:
                self.call("exp", "abandon", target, "--reason", "it cannot run as drafted")
        elif stage == "outdated":
            self.call("exp", "abandon", target, "--reason", "its reference no longer counts")
        elif stage == "confirm":
            self.call("exp", "reproduce", target, "--split", action["split"])
        elif stage == "judge":
            self.judge(target)
        elif stage == "unfinished":
            self.worktree(p.record(target)["track"])
            self.call("exp", "judge", target)
        elif stage == "crashed":
            rec = p.record(target)
            self.worktree(rec["track"])
            again = len(rec["attempts"]) < p.ledger.cfg.max_attempts and "judge it as a crash" not in action["why"] and rng.random() < 0.5
            if not again or self.run(target) == 1:
                self.judge(target, "crash")
        elif stage in ("interrupted", "resume"):
            self.worktree(p.record(target)["track"])
            choice = rng.random()
            if choice < 0.7:
                if self.run(target) == 1:
                    self.judge(target, "crash")
            elif choice < 0.85:
                self.call("exp", "fail", target, "--reason", "the saved state is not trusted")
            else:
                self.judge(target, "crash")
        else:
            raise AssertionError(f"no way on from stage {stage}\n{self.story()}")
        assert exps is not None
        return True

    # --- what goes wrong in between ----------------------------------------------------

    def disrupt(self) -> None:
        p, rng = self.p, self.rng
        exps, tracks = p.ledger.experiments(), p.ledger.tracks()
        kind = rng.choice(["cut", "cut", "half", "pause", "contract", "refreeze", "folder", "gone", "upgrade", "typed", "typed", "typed", "typed"])
        if kind in ("cut", "half"):
            drafts = [e for e in exps if e.status in ("draft", "crashed") and e.type in ("tune", "baseline") and p.ledger.worktree_path(e.track).exists()]
            if drafts and not p.ledger.paused() and self.damaged is None:
                eid = rng.choice(drafts).id
                p.fill(p.ledger.exp_dir(eid) / "design.md")
                if kind == "cut" or p.record(eid)["attempts"]:
                    cut_off(p, eid)  # the machine went down in the middle of its run
                    self.log.append(f"# {eid} cut off in the middle of its run")
                else:
                    pause_record(p, eid)  # somebody paused it and went home
                    self.log.append(f"# {eid} paused in the middle of its run")
        elif kind == "refreeze":
            if self.damaged is None and p.ledger.lock_path.exists() and not p.ledger.paused():
                evaluator = p.research / "eval" / "evaluate.py"
                before = evaluator.read_text()
                evaluator.write_text(before + f"\n# stricter, {self.name('change ')}\n")
                self.person(True)  # the owner of the program changes the evaluator and freezes again
                if self.call("freeze", "--reason", "a stricter evaluator", "--yes")[0] != 0:
                    evaluator.write_text(before)  # refused (the environment is gone): they take the change back
                self.person(False)
        elif kind == "pause":
            self.person(rng.random() < 0.5)  # in the chat, or by the person from a terminal
            self.call("pause", "--reason", "the machine is needed", *(["--note", "where I was"] if rng.random() < 0.5 else []))
            self.person(False)
        elif kind == "contract":
            if self.damaged is None and p.ledger.lock_path.exists():
                path = p.research / rng.choice(["eval/evaluate.py", "PROGRAM.md", "arl.toml"])
                self.damaged = (path, path.read_text())
                path.write_text(path.read_text() + "\n# edited by accident\n")
                self.log.append(f"# {path.name} edited by hand")
        elif kind == "gone":
            if self.env.exists():
                self.env.rename(self.away)
                self.log.append("# the environment the contract names is moved away")
        elif kind == "upgrade":
            if self.env.exists():
                install(self.env, "demo", f"{self.name('')}.0")
                self.log.append("# a package in the environment changes version")
        elif kind == "folder":
            there = [t.name for t in tracks if p.ledger.worktree_path(t.name).exists()]
            clean = [name for name in there if not git(p.ledger.worktree_path(name), "status", "--porcelain")]
            if clean:
                name = rng.choice(clean)
                shutil.rmtree(p.ledger.worktree_path(name))
                self.log.append(f"# the worktree folder of {name} removed by hand")
        else:
            eid = rng.choice([e.id for e in exps] + ["E0999"])
            hid = rng.choice([h.id for h in p.ledger.hypotheses()] + ["H999"])
            nid = rng.choice([n.id for n in p.ledger.introspections()] + ["N999"])
            track = rng.choice([t.name for t in tracks] + ["nowhere"])
            self.call(
                *rng.choice(
                    [
                        ("exp", "run", eid),
                        ("exp", "judge", eid, "--verdict", rng.choice(["keep", "discard", "finding", "crash"]), "--reason", "typed without looking"),
                        ("exp", "abandon", eid, "--reason", "typed without looking"),
                        ("exp", "fail", eid, "--reason", "typed without looking"),
                        ("exp", "commit", eid, "-m", "typed without looking"),
                        ("exp", "show", eid),
                        ("exp", "env", eid),
                        ("exp", "env", eid, rng.choice([e.id for e in exps] + ["E0999"])),
                        ("exp", "log", eid),
                        ("exp", "pause"),
                        ("exp", "reproduce", eid),
                        ("exp", "reproduce", eid, "--split", "confirm"),
                        ("exp", "new", "--type", rng.choice(rules.TYPES), "--track", track, "--title", self.name("typed "), "--hyp", hid),
                        ("exp", "new", "--type", "baseline", "--track", track, "--title", self.name("typed ")),
                        ("track", "set", track, "--status", rng.choice(["active", "parked", "closed"]), "--note", "typed"),
                        ("track", "set", track, "--status", "parked", "--note", "typed", "--introspection", nid),
                        ("track", "new", self.name("typed")),
                        ("track", "new", self.name("typed"), "--introspection", nid),
                        ("track", "new", self.name("typed"), "--from", eid),
                        ("track", "sync", track, "--note", "typed"),
                        ("track", "worktree", track),
                        ("track", "list"),
                        ("introspect", "new"),
                        ("introspect", "due"),
                        ("introspect", "close", nid, "--decision", rng.choice(DECISIONS), *NOTHING_NEW),
                        ("introspect", "resolve", nid, "--decision", rng.choice(DECISIONS), "--note", "typed"),
                        ("hyp", "set", hid, "--status", rng.choice(["open", "supported", "refuted", "inconclusive", "dropped"]), "--note", "typed"),
                        ("hyp", "list"),
                        ("idea", "add", "--title", self.name("idea "), "--body", "typed"),
                        ("idea", "set", "I001", "--status", rng.choice(["open", "promoted", "parked", "rejected"]), "--note", "typed"),
                        ("lesson", "add", "--scope", "project", "--title", self.name("lesson "), "--body", "typed"),
                        ("lesson", "list"),
                        ("note", "--about", rng.choice(["work", "problem"]), "typed in passing"),
                        ("reload",),
                        ("resume",),
                        ("verify",),
                        ("status", "--json"),
                        ("freeze", "--reason", "typed without looking"),
                    ]
                )
            )

    # --- the walk -------------------------------------------------------------------------

    def go(self, steps: int) -> None:
        stuck = 0
        for _ in range(steps):
            if self.rng.random() < 0.25:
                self.disrupt()
                self.check()
                continue
            action = self.next()
            before = self.fingerprint()
            self.log.append(f"# next: {action['stage']} {action.get('target', '')} ({action['why']})")
            self.seen[action["stage"]] += 1
            if not self.follow(action):
                assert action.get("stop"), self.story()
                break
            self.check()
            stuck = stuck + 1 if self.fingerprint() == before else 0
            if stuck >= 3:
                raise DeadEnd(f"three times '{action['stage']} {action.get('target', '')}' and nothing moved:\n{self.story()}")

    def settle(self) -> None:
        """Put back what was pulled away, and let the loop come to rest."""
        if self.damaged:
            path, text = self.damaged
            path.write_text(text)
            self.damaged = None
        if self.away.exists():
            self.away.rename(self.env)
        if self.p.ledger.paused():
            self.person(True)
            self.call("resume")
            self.person(False)
        for t in self.p.ledger.tracks():
            self.worktree(t.name)
        for e in self.p.ledger.experiments():
            if e.status == "judging":
                assert "cut off before it was complete" in self.call("verify")[1], self.story()
                assert self.call("exp", "judge", e.id)[0] == 0, self.story()


@pytest.mark.parametrize("seed", SEEDS)
def test_walk(project: Project, monkeypatch, seed: int):
    p = project
    env = make_env(p.base / "env")
    install(env, "demo", "1.0")
    # Every second walk has a target that some of its results reach: some hold on the other
    # split and end the walk, some do not. The other walks go as deep as they can.
    target = "target = 0.95" if seed % 2 else ""
    p.setup(every=5, stagnation=3, max_experiments=max(30, STEPS // 2), python="python", extra=env_table(env), target=target)
    walk = Walk(p, seed, monkeypatch, env)
    walk.go(STEPS)
    walk.settle()
    code, text = walk.call("verify")
    assert (code, text.strip()) == (0, "ok"), walk.story()
    assert walk.call("next")[0] == 0
    if os.environ.get("ARL_WALK_REPORT"):  # which stages the walks reached, one line per walk
        statuses = collections.Counter(e.status for e in p.ledger.experiments())
        with open(os.environ["ARL_WALK_REPORT"], "a") as f:
            f.write(json.dumps({"seed": seed, "seen": walk.seen, "experiments": statuses}) + "\n")
