"""Stopping the loop and going on, and what a session reads and leaves behind:
pause, resume, reload, notes and lessons."""

from __future__ import annotations

import argparse

from .. import render, rules
from ..ledger import SCOPES, Ledger
from ..util import ArlError, now, one_line, write_front, write_json
from . import common, jobs
from .common import ARL


AT_TERMINAL = "a person at a terminal"


def cmd_pause(args: argparse.Namespace) -> int:
    """Stop the loop where it is, to be continued later."""
    ledger = Ledger.locate()
    with ledger.locked():
        state = ledger.paused()
        if state is not None:
            if not args.note:
                raise ArlError(
                    f"the loop is paused already. To add to the note for whoever continues: {ARL} pause --note \"...\"\n"
                    f"To continue, when the person says so: {ARL} resume"
                )
            state["note"] = f"{state['note']} {args.note}" if state.get("note") else args.note
            write_json(ledger.paused_path, state)
            ledger.log_activity("paused", f"note added: {args.note}")
            if ledger.lock_path.exists():
                common.finish(ledger, "pause, note added")
            print("The note is kept with the pause and printed again at 'arl resume'.")
            return 0
        # A stop is never refused for a missing word: what is not said is recorded as such.
        reason = args.reason or "not given"
        state = {"at": now(), "by": AT_TERMINAL if common.interactive() else "the agent", "reason": reason, "note": args.note or ""}
        write_json(ledger.paused_path, state)
        asked = jobs.ask_to_pause(ledger)
        ledger.log_activity("paused", f"{reason}. {args.note or ''}".strip())
        if ledger.lock_path.exists():
            common.finish(ledger, "pause")
    print("The loop is paused. Nothing starts, runs or is judged until 'arl resume'.")
    jobs.say_asked(ledger, asked)
    if not args.note:
        print("No note was left. If you were in the middle of something, say where, now. The next session starts")
        print(f"from the files and that note:  {ARL} pause --note \"where I was, what comes next\"")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Continue a paused loop: print what was left behind and what there is to read again."""
    ledger = Ledger.locate()
    with ledger.locked():
        state = ledger.paused()
        if state is None:
            raise ArlError("the loop is not paused")
        if state.get("by") == AT_TERMINAL and not common.interactive():
            raise ArlError(
                "The loop was paused by a person at a terminal, and it is continued the same way:\n"
                f"they run '{ARL} resume' there. Until then nothing starts, runs or is judged.\n"
                "Notes, ideas and lessons can still be written down."
            )
        ledger.paused_path.unlink()
        ledger.log_activity("resumed", f"paused {state.get('at', '')} ({state.get('reason', '')}). Note: {state.get('note', '')}")
        _mark_reloaded(ledger)
        if ledger.lock_path.exists():
            common.finish(ledger, "resume")
    print(render.reload_text(ledger, state, common.env_note(ledger)), end="")
    print()
    print(render.next_text(ledger, common.next_action(ledger)), end="")
    return 0


def _mark_reloaded(ledger: Ledger) -> str | None:
    nid = rules.introspection_state(ledger)["reload"]
    if nid:
        meta, body = ledger.introspection(nid)
        meta["reloaded"] = now()
        write_front(ledger.intro_path(nid), meta, body)
    ledger.mark_reloaded()
    return nid


def cmd_reload(args: argparse.Namespace) -> int:
    """Print the knowledge and the lessons as they are now. Part 4 of an introspection."""
    ledger = Ledger.locate()
    with ledger.locked():
        nid = _mark_reloaded(ledger)
        if nid:
            common.finish(ledger, f"{nid} reloaded")
    print(render.reload_text(ledger, env=common.env_note(ledger)), end="")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    """Write down, when it happens, something an introspection should take up."""
    ledger = Ledger.locate()
    if not ledger.exists():
        raise ArlError(f"no {ledger.config_path}. Run 'arl init' in the project first.")
    text = one_line(" ".join(args.text), "the note")
    ledger.log_activity("note", text, about=args.about)
    where = "part 2 (how the work went)" if args.about == "work" else "part 3 (what we know about the problem)"
    print(f"Noted for the next introspection, {where}.")
    return 0


def cmd_lesson_add(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        lid = ledger.add_lesson(args.scope, args.title, args.body, args.source or "")
        path = ledger.lessons_file(args.scope)
        if args.scope == "project" and ledger.lock_path.exists():
            common.finish(ledger, f"lesson {lid}")
        # A general lesson is not committed anywhere. Its file sits next to the skill, on this
        # machine only: what a loop writes in one project never goes into the arl repository.
    print(f"{lid} added to {path}")
    return 0


def cmd_lesson_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rows = [[l["id"], scope, l["title"]] for scope in SCOPES if args.scope in (None, scope) for l in ledger.lessons(scope)]
    print(render.table(["id", "scope", "title"], rows), end="")
    return 0
