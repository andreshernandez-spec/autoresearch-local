"""Command line: the parser, and what happens to what was typed.

The commands themselves are in arl.commands, one module per area."""

from __future__ import annotations

import argparse
import contextlib
import signal
import sys
from typing import Any

from . import rules
from .commands import experiments, hypotheses, introspections, project, runs, session, tracks
from .config import DECISIONS
from .ledger import HYP_STATES, IDEA_STATES, SCOPES, Ledger
from .util import EXIT_BUSY, ArlError, check_text, one_line


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arl", description="Local harness for automated research.")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def add(parent: Any, name: str, func: Any, help: str) -> argparse.ArgumentParser:
        sp = parent.add_parser(name, help=help, description=help)
        sp.set_defaults(func=func)
        return sp

    add(sub, "init", project.cmd_init, "Create research/ with templates in this project.")
    sp = add(sub, "freeze", project.cmd_freeze, "Hash the contract into frozen.lock. After the first experiment only a person can.")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--yes", action="store_true", help="skip the question (still needs a terminal)")
    sp = add(sub, "status", project.cmd_status, "Where things stand.")
    sp.add_argument("--json", action="store_true")
    sp = add(sub, "next", project.cmd_next, "The one thing to do now, with instructions.")
    sp.add_argument("--json", action="store_true")
    add(sub, "verify", project.cmd_verify, "Check that ledger, contract and git agree.")
    add(sub, "reload", session.cmd_reload, "Print the knowledge and the lessons as they are now. Read them.")
    sp = add(sub, "pause", session.cmd_pause, "Stop the loop where it is: a run saves its state and stops, nothing new starts.")
    sp.add_argument("--reason", help="why it is paused")
    sp.add_argument("--note", help="where you were and what you were about to do, for whoever continues. Can be added after the pause")
    add(sub, "resume", session.cmd_resume, "Continue a paused loop, when the person says so.")
    sp = add(sub, "note", session.cmd_note, "Write down now what the next introspection should take up.")
    sp.add_argument("--about", required=True, choices=["work", "problem"], help="work: a slip or waste in the way of working. problem: something seen about the problem")
    sp.add_argument("text", nargs="+")

    track = sub.add_parser("track", help="Code tracks: one branch and worktree each.").add_subparsers(dest="action", required=True)
    sp = add(track, "new", tracks.cmd_track_new, "Open a track.")
    sp.add_argument("name")
    sp.add_argument("--from", dest="from_ref", help="commit, branch or experiment (E0007: its code and its params) to start from. Default: the home branch")
    sp.add_argument("--introspection", help="the latest closed introspection, which decided to open it")
    sp.add_argument("--params", help="JSON file with the track's starting params")
    sp = add(track, "set", tracks.cmd_track_set, "Park, close or reactivate a track.")
    sp.add_argument("name")
    sp.add_argument("--status", required=True, choices=["active", "parked", "closed"])
    sp.add_argument("--note", required=True)
    sp.add_argument("--introspection", help="the latest closed introspection, which decided it")
    sp = add(track, "sync", tracks.cmd_track_sync, "Record that a track branch was changed by hand.")
    sp.add_argument("name")
    sp.add_argument("--note", required=True)
    sp = add(track, "worktree", tracks.cmd_track_worktree, "Put a track's worktree back when its folder is gone, and its branch after a clone.")
    sp.add_argument("name")
    add(track, "list", tracks.cmd_track_list, "List tracks.")

    hyp = sub.add_parser("hyp", help="Hypotheses: claims, separate from the experiments that test them.").add_subparsers(dest="action", required=True)
    sp = add(hyp, "new", hypotheses.cmd_hyp_new, "Start a hypothesis file.")
    sp.add_argument("--title", required=True)
    sp.add_argument("--from", dest="source", help="where it comes from (an experiment, an idea, an introspection)")
    sp = add(hyp, "set", hypotheses.cmd_hyp_set, "Change a hypothesis's status.")
    sp.add_argument("id")
    sp.add_argument("--status", required=True, choices=HYP_STATES)
    sp.add_argument("--note", required=True)
    sp = add(hyp, "list", hypotheses.cmd_hyp_list, "List hypotheses.")
    sp.add_argument("--status", choices=HYP_STATES)

    idea = sub.add_parser("idea", help="IDEAS.md: what could change the path, read at every introspection.").add_subparsers(dest="action", required=True)
    sp = add(idea, "add", hypotheses.cmd_idea_add, "Write an idea down.")
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", required=True, help="what it is, why it could matter, the cheapest test")
    sp.add_argument("--from", dest="source", help="where it came up")
    sp.add_argument("--kind", default="approach", help="approach, feature, diagnostic, evaluator, external ...")
    sp = add(idea, "set", hypotheses.cmd_idea_set, "Change an idea's status.")
    sp.add_argument("id")
    sp.add_argument("--status", required=True, choices=IDEA_STATES)
    sp.add_argument("--note", required=True)
    sp = add(idea, "list", hypotheses.cmd_idea_list, "List ideas.")
    sp.add_argument("--status", choices=IDEA_STATES)

    lesson = sub.add_parser("lesson", help="Lessons about the way of working, kept in the instructions agents load.").add_subparsers(dest="action", required=True)
    sp = add(lesson, "add", session.cmd_lesson_add, "Record a lesson.")
    sp.add_argument("--scope", required=True, choices=SCOPES, help="general: any project that uses arl. project: only this one")
    sp.add_argument("--title", required=True, help="the instruction, in a few words")
    sp.add_argument("--body", required=True, help="what happened, and what to do instead")
    sp.add_argument("--from", dest="source", help="the introspection or experiment it comes from")
    sp = add(lesson, "list", session.cmd_lesson_list, "List lessons.")
    sp.add_argument("--scope", choices=SCOPES)

    exp = sub.add_parser("exp", help="Experiments: design, run, judge.").add_subparsers(dest="action", required=True)
    sp = add(exp, "new", experiments.cmd_exp_new, "Draft an experiment.")
    sp.add_argument("--type", required=True, choices=rules.TYPES)
    sp.add_argument("--track", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--hyp", action="append", help="hypothesis it tests (repeatable)")
    sp.add_argument("--command", help="diagnose only: the command to run instead of the one in arl.toml")
    sp = add(exp, "commit", experiments.cmd_exp_commit, "Commit the code of a feature or diagnose experiment.")
    sp.add_argument("id")
    sp.add_argument("-m", "--message", required=True)
    sp = add(exp, "run", runs.cmd_exp_run, "Lock the design, run, evaluate. Continues a run that was paused.")
    sp.add_argument("id")
    sp.add_argument("--allow-duplicate", metavar="WHY", help="repeat a known result on purpose")
    sp = add(exp, "pause", experiments.cmd_exp_pause, "Ask the run that is going to save its state and stop.")
    sp.add_argument("id", nargs="?")
    sp = add(exp, "fail", experiments.cmd_exp_fail, "Give up a paused run: the attempt counts as failed, the next run starts from nothing.")
    sp.add_argument("id")
    sp.add_argument("--reason", required=True)
    sp = add(exp, "judge", experiments.cmd_exp_judge, "Record the verdict.")
    sp.add_argument("id")
    sp.add_argument("--verdict", choices=["keep", "discard", "finding", "crash"])
    sp.add_argument("--reason")
    sp.add_argument("--hyp", action="append", metavar="ID=STATE", help="what the run did to a hypothesis (repeatable)")
    sp.add_argument("--simpler", action="store_true", help="keep a neutral result because it simplifies")
    sp = add(exp, "abandon", experiments.cmd_exp_abandon, "Drop a draft that never ran.")
    sp.add_argument("id")
    sp.add_argument("--reason", required=True)
    sp = add(exp, "show", experiments.cmd_exp_show, "One experiment: conditions, result, verdict.")
    sp.add_argument("id")
    sp.add_argument("--json", action="store_true")
    sp = add(exp, "list", experiments.cmd_exp_list, "List experiments.")
    sp.add_argument("--track")
    sp.add_argument("--status")
    sp = add(exp, "log", experiments.cmd_exp_log, "The end of the latest log.")
    sp.add_argument("id")
    sp.add_argument("--eval", action="store_true", help="the evaluator's log")
    sp.add_argument("--lines", type=int, default=60)
    sp = add(exp, "env", experiments.cmd_exp_env, "The environment an experiment ran in, and what differs from now or from another experiment.")
    sp.add_argument("id")
    sp.add_argument("other", nargs="?", help="another experiment to compare with (default: the environment as it is now)")
    sp = add(exp, "reproduce", runs.cmd_exp_reproduce, "Run a recorded experiment again from its commit and params.")
    sp.add_argument("id")
    sp.add_argument("--split", help="score on another split named in arl.toml")

    intro = sub.add_parser("introspect", help="The periodic introspection: the path, the way of working, what is known.").add_subparsers(dest="action", required=True)
    sp = add(intro, "due", introspections.cmd_introspect_due, "Is an introspection due, and why.")
    sp.add_argument("--json", action="store_true")
    sp = add(intro, "new", introspections.cmd_introspect_new, "Open an introspection with the facts filled in.")
    sp.add_argument("--trigger", help="why now, when it is not one of the counters")
    sp = add(intro, "close", introspections.cmd_introspect_close, "Record the decision, once all parts are done.")
    sp.add_argument("id")
    sp.add_argument("--decision", required=True, choices=DECISIONS)
    sp.add_argument("--note")
    sp.add_argument("--no-lessons", metavar="WHY", help="part 2 found nothing worth a standing instruction")
    sp.add_argument("--knowledge-unchanged", metavar="WHY", help="part 3 found nothing new about the problem")
    sp = add(intro, "resolve", introspections.cmd_introspect_resolve, "A person decides a waiting introspection (terminal only).")
    sp.add_argument("id")
    sp.add_argument("--decision", required=True, choices=DECISIONS)
    sp.add_argument("--note", required=True)
    return p


_ONE_LINE = ("title", "reason", "note", "trigger", "no_lessons", "knowledge_unchanged", "allow_duplicate", "message", "source")


def _clean(args: argparse.Namespace) -> None:
    """Everything typed has to be text that can be stored. What is one line is made one line,
    and what was asked for cannot be blank."""
    for name, value in vars(args).items():
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                check_text(item, f"--{name.replace('_', '-')}")
        if name in _ONE_LINE and isinstance(value, str):
            setattr(args, name, one_line(value, f"--{name.replace('_', '-')}"))


def _on_signal(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    try:
        # Asked to stop in any of these ways, a run saves its state and takes its job down with it.
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, _on_signal)
    except ValueError:
        pass
    try:
        _clean(args)
        return args.func(args) or 0
    except ArlError as e:
        print(f"arl: {e}", file=sys.stderr)
        if e.code != EXIT_BUSY:
            with contextlib.suppress(Exception):  # what was refused is material for the next introspection
                ledger = Ledger.locate()
                if ledger.exists():
                    ledger.log_activity("refused", str(e), command=" ".join(argv))
        return e.code
    except KeyboardInterrupt:
        print("arl: interrupted", file=sys.stderr)
        return 130
