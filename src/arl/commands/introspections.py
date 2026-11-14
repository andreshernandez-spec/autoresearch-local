"""The periodic introspection: whether one is due, opening it with the facts,
closing it with a decision."""

from __future__ import annotations

import argparse
import json

from .. import contract, render, rules
from ..config import DECISIONS
from ..ledger import Ledger
from ..util import ArlError, now, one_line, require_filled, write_front, write_text
from . import common
from .common import ARL


def cmd_introspect_due(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    state = rules.introspection_state(ledger)
    if args.json:
        print(json.dumps(state, indent=2))
    elif state["due"]:
        print("An introspection is due:\n  " + "\n  ".join(state["reasons"]))
    else:
        print(f"No introspection due: {state['judged_since']} of {ledger.cfg.intro_every} experiments judged since {state['last'] or 'the start'}.")
    return 0


def cmd_introspect_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with common.writing(ledger):
        contract.require(ledger)
        common.gate_paused(ledger)
        state = rules.introspection_state(ledger)
        if state["open"] or state["waiting"]:
            raise ArlError(f"{state['open'] or state['waiting']} is not finished yet")
        pending = [e["id"] for e in ledger.experiments() if e["status"] in ("running", "evaluated", "judging")]
        if pending:
            raise ArlError(f"finish {', '.join(pending)} first: an introspection needs their verdicts")
        nid = ledger.next_id("N")
        trigger = one_line(args.trigger, "--trigger") if args.trigger else "; ".join(state["reasons"]) or "asked for"
        ledger.intro_root.mkdir(parents=True, exist_ok=True)
        events = ledger.activity()
        earlier = [n for n in ledger.introspections() if "activity_upto" in n]
        activity_from = earlier[-1]["activity_upto"] if earlier else 0
        meta = {
            "id": nid,
            "status": "open",
            "trigger": trigger,
            "opened": now(),
            "window": state["window"],
            "unconfirmed": state["unconfirmed"],
            "activity_upto": len(events),
            "knowledge_sha256": ledger.knowledge_hash(),
            "lessons_general": len(ledger.lessons("general")),
            "lessons_project": len(ledger.lessons("project")),
        }
        facts = render.introspection_facts(ledger, trigger, state, activity_from)
        write_front(ledger.intro_root / f"{nid}.md", meta, render.fill(render.template("introspection.md"), id=nid, facts=facts))
        write_text(ledger.status_path, render.status(ledger))
    print(f"{nid} opened: {ledger.intro_root / f'{nid}.md'}")
    print("The facts are filled in. Write its parts with fresh eyes, below the +++ block:")
    print("  part 1, the path; part 2, lessons about the way of working ('arl lesson add');")
    print(f"  part 3, what is now known about the problem (edit {ledger.knowledge_path}).")
    print(f"Then:  {ARL} introspect close {nid} --decision <{'|'.join(DECISIONS)}>")
    print(f"'{ARL} next' prints the full steps.")
    return 0


def cmd_introspect_close(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with common.writing(ledger):
        contract.require(ledger)  # who may decide what is in arl.toml: it has to be the frozen one
        common.gate_paused(ledger)
        meta, body = ledger.introspection(args.id)
        if meta["status"] != "open":
            raise ArlError(f"{args.id} is {meta['status']}")
        require_filled(ledger.intro_path(args.id), f"introspection {args.id}")
        added = len(ledger.lessons("general")) - meta.get("lessons_general", 0) + len(ledger.lessons("project")) - meta.get("lessons_project", 0)
        if added <= 0 and not args.no_lessons:
            raise ArlError(
                f"Part 2 of {args.id} is not done: no lesson was recorded since it was opened.\n"
                f"Record what deserves a standing instruction ('{ARL} lesson add --scope general|project ...'),\n"
                "or pass --no-lessons \"why\" if this window taught nothing about the way of working."
            )
        same_knowledge = ledger.knowledge_hash() == meta.get("knowledge_sha256")
        if same_knowledge and not args.knowledge_unchanged:
            raise ArlError(
                f"Part 3 of {args.id} is not done: {ledger.knowledge_path} is as it was when the introspection was opened.\n"
                "Bring it up to date with what this window showed about the problem (revise and merge, do not append),\n"
                "or pass --knowledge-unchanged \"why\" if the window showed nothing new."
            )
        reopening = rules.introspection_state(ledger)["last_decision"] == "stop" and args.decision != "stop"
        alone = args.decision in ledger.cfg.agent_may and not reopening
        meta.update(status="closed" if alone else "waiting", decision=args.decision, note=args.note or "", lessons_added=max(added, 0), knowledge_changed=not same_knowledge)
        if args.no_lessons:
            meta["no_lessons"] = args.no_lessons
        if args.knowledge_unchanged:
            meta["knowledge_unchanged"] = args.knowledge_unchanged
        if alone:
            meta.update(closed=now(), decided_by="agent")
        else:
            meta["proposed"] = now()
        write_front(ledger.intro_path(args.id), meta, body)
        common.finish(ledger, f"{args.id} introspection, {args.decision}" + ("" if alone else " proposed"))
    if alone:
        print(f"{args.id} closed: {args.decision}.")
    elif reopening:
        print(f"{args.id} proposes '{args.decision}' after a stop. Reopening the program is a person's decision.")
    else:
        print(f"{args.id} proposes '{args.decision}', which the agent may not decide alone.")
    print()
    print(render.next_text(ledger, common.next_action(ledger)), end="")
    return 0


def cmd_introspect_resolve(args: argparse.Namespace) -> int:
    if not common.interactive():
        raise ArlError("a waiting introspection is resolved by a person, in a terminal")
    ledger = Ledger.locate()
    with common.writing(ledger):
        contract.require(ledger)
        meta, body = ledger.introspection(args.id)
        if meta["status"] != "waiting":
            raise ArlError(f"{args.id} is {meta['status']}, not waiting")
        meta.update(status="closed", proposed_decision=meta["decision"], decision=args.decision, closed=now(), decided_by="person", note=args.note)
        write_front(ledger.intro_path(args.id), meta, body.rstrip("\n") + f"\n\n# Resolved\n\n{now()[:10]}: {args.decision}. {args.note}\n")
        common.finish(ledger, f"{args.id} resolved, {args.decision}")
    print(f"{args.id} closed: {args.decision}.")
    return 0
