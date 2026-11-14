"""Hypotheses and ideas: what is claimed, and what could be tried."""

from __future__ import annotations

import argparse

from .. import render
from ..ledger import Ledger, save_front
from ..record import Hypothesis
from ..util import now, one_line, slugify
from . import common


def cmd_hyp_new(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        hid = ledger.next_id("H")
        ledger.hyp_root.mkdir(parents=True, exist_ok=True)
        path = ledger.hyp_root / f"{hid}-{slugify(args.title)}.md"
        source = one_line(args.source, "--from") if args.source else ""
        meta = Hypothesis(id=hid, title=args.title, status="open", created=now(), updated=now(), source=source)
        save_front(path, meta, render.fill(render.template("hypothesis.md"), id=hid, title=args.title))
    print(f"{hid}: write {path} (below the +++ block, which is arl's)")
    return 0


def cmd_hyp_set(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        ledger.set_hypothesis(args.id, args.status, args.note)
        common.finish(ledger, f"{args.id} {args.status}")
    print(f"{args.id} is {args.status}.")
    return 0


def cmd_hyp_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rows = [[h.id, h.status, h.title] for h in ledger.hypotheses() if not args.status or h.status == args.status]
    print(render.table(["id", "status", "title"], rows), end="")
    return 0


# --- ideas ------------------------------------------------------------------------


def cmd_idea_add(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        iid = ledger.add_idea(args.title, args.source or "", args.kind, args.body)
        if ledger.lock_path.exists():
            common.finish(ledger, f"idea {iid}")
    print(f"{iid} added to {ledger.ideas_path}")
    return 0


def cmd_idea_set(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    with ledger.locked():
        ledger.set_idea(args.id, args.status, args.note)
        common.finish(ledger, f"idea {args.id} {args.status}")
    print(f"{args.id} is {args.status}.")
    return 0


def cmd_idea_list(args: argparse.Namespace) -> int:
    ledger = Ledger.locate()
    rows = [[i["id"], i["status"], i["title"]] for i in ledger.ideas() if not args.status or i["status"] == args.status]
    print(render.table(["id", "status", "title"], rows), end="")
    return 0
