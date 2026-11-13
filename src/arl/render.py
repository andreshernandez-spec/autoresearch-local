"""Text the harness writes: STATUS.md, the facts in a review or an analysis, the briefs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import contract, rules
from .ledger import Ledger
from .util import fmt, now

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "bin" / "arl"
TEMPLATES = Path(__file__).parent / "templates"


def template(name: str) -> str:
    return (TEMPLATES / name).read_text()


def fill(text: str, **values: Any) -> str:
    """Replace {key} for the given keys only, so other braces survive."""
    if not values:
        return text
    pattern = re.compile(r"\{(" + "|".join(map(re.escape, values)) + r")\}")
    return pattern.sub(lambda m: str(values[m.group(1)]), text)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "none\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


# --- experiments and tracks as tables ---------------------------------------------


def _primary(ledger: Ledger, rec: dict[str, Any] | None) -> str:
    name = ledger.cfg.primary.name
    metrics = (rec or {}).get("metrics") or {}
    return fmt(metrics[name]) if name in metrics else ""


def _gain(rec: dict[str, Any]) -> str:
    gain = rec.get("comparison", {}).get("gain")
    return f"{gain:+.6g}" if gain is not None else ""


def _verdict(rec: dict[str, Any]) -> str:
    return rec["verdict"]["decision"] if rec["status"] == "judged" else f"({rec['status']})"


def exp_table(ledger: Ledger, exps: list[dict[str, Any]]) -> str:
    rows = [
        [
            e["id"],
            e["track"],
            e["type"],
            " ".join(e["hypotheses"]),
            _primary(ledger, e),
            _gain(e),
            e.get("comparison", {}).get("outcome", ""),
            _verdict(e),
            e["title"],
        ]
        for e in exps
    ]
    head = ["id", "track", "type", "tests", ledger.cfg.primary.name, "gain", "outcome", "verdict", "title"]
    return table(head, rows)


def track_table(ledger: Ledger, lock: dict[str, Any] | None) -> str:
    exps = ledger.experiments()
    rows = []
    for t in ledger.tracks():
        mine = [e for e in exps if e["track"] == t["name"]]
        ref = rules.reference_for(ledger, t, lock["eval_digest"]) if lock else None
        scored = [e for e in mine if e["status"] == "judged" and e["type"] in ("tune", "feature")]
        run = 0
        for e in sorted(scored, key=lambda e: e["verdict"]["at"]):
            run = 0 if e["verdict"]["decision"] == "keep" else run + 1
        rows.append(
            [
                t["name"],
                t["status"],
                ref["id"] if ref else "none",
                _primary(ledger, ref),
                len(mine),
                sum(rules.kept(e) for e in mine),
                run,
                t["head_commit"][:10],
            ]
        )
    head = ["track", "status", "reference", ledger.cfg.primary.name, "experiments", "kept", "in a row without a keep", "head"]
    return table(head, rows)


def _budget_line(ledger: Ledger) -> str:
    b = rules.budget(ledger)
    return (
        f"{b['experiments']} of {b['max_experiments'] if b['max_experiments'] is not None else 'unlimited'} experiments, "
        f"{b['hours']} of {b['max_run_hours'] if b['max_run_hours'] is not None else 'unlimited'} run hours"
    )


# --- STATUS.md --------------------------------------------------------------------


def status(ledger: Ledger) -> str:
    cfg = ledger.cfg
    lock = contract.load(ledger)
    out = ["# Status", "", "Written by arl after every change. Do not edit.", ""]
    if lock:
        who = "" if lock.get("interactive") else " (frozen without a terminal, not yet confirmed by a person)"
        out.append(f"- Contract v{lock['version']}, frozen {lock['frozen_at'][:10]}{who}.")
        bad = contract.problems(ledger)
        if bad:
            out.append(f"- CONTRACT DOES NOT MATCH THE FILES: {'; '.join(bad)}")
    else:
        out.append("- Contract: not frozen.")
    out.append(f"- Budget: {_budget_line(ledger)}.")
    rs = rules.review_state(ledger)
    last = f"{rs['last']} decided '{rs['last_decision']}'" if rs["last"] else "none so far"
    due = "DUE: " + "; ".join(rs["reasons"]) if rs["due"] else f"{rs['judged_since']} of {cfg.review_every} experiments judged since"
    out.append(f"- Reviews: {last}. {due}.")
    action = rules.next_action(ledger)
    target = f" {action['target']}" if action.get("target") else ""
    out.append(f"- Next: {action['stage']}{target} ({action['why']}).")

    out += ["", "## Tracks", "", track_table(ledger, lock)]
    exps = ledger.experiments()
    flying = [e for e in exps if e["status"] in rules.IN_FLIGHT]
    out += ["## In flight", "", table(["id", "track", "type", "status", "title"], [[e["id"], e["track"], e["type"], e["status"], e["title"]] for e in flying])]
    latest = list(reversed(exps))[:20]
    out += [f"## Experiments (latest {len(latest)} of {len(exps)})", "", exp_table(ledger, latest)]

    tested: dict[str, list[str]] = {}
    for e in exps:
        for h in e["hypotheses"]:
            tested.setdefault(h, []).append(e["id"])
    hyps = ledger.hypotheses()
    rows = [[h["id"], h["status"], h["title"], " ".join(tested.get(h["id"], []))] for h in hyps]
    out += ["## Hypotheses", "", table(["id", "status", "title", "tested by"], rows)]

    ideas = ledger.ideas()
    counts = ", ".join(f"{sum(i['status'] == s for i in ideas)} {s}" for s in ("open", "promoted", "parked", "rejected"))
    out += ["## Ideas", "", f"{counts}. Full text in IDEAS.md.", ""]
    out += [f"- {i['id']} {i['title']}" for i in ideas if i["status"] == "open"]
    return "\n".join(out).rstrip("\n") + "\n"


def state(ledger: Ledger) -> dict[str, Any]:
    """The same picture for a program."""
    lock = contract.load(ledger)
    return {
        "home": str(ledger.home),
        "contract": {"version": lock["version"], "problems": contract.problems(ledger)} if lock else None,
        "budget": rules.budget(ledger),
        "review": rules.review_state(ledger),
        "next": rules.next_action(ledger),
        "tracks": ledger.tracks(),
        "experiments": [
            {k: e.get(k) for k in ("id", "title", "type", "track", "status", "hypotheses", "metrics", "comparison", "verdict")}
            for e in ledger.experiments()
        ],
        "hypotheses": ledger.hypotheses(),
        "ideas": ledger.ideas(),
    }


# --- facts written into records ---------------------------------------------------


def analysis_facts(ledger: Ledger, rec: dict[str, Any]) -> str:
    cfg = ledger.cfg
    attempt = rec["attempts"][-1]
    where = f"Code {rec['code_commit'][:10]} on `{rec['branch']}`, contract v{rec['contract']['version']}."
    took = f"Run {attempt.get('run_seconds', 0):.0f} s"
    if "eval_seconds" in attempt:
        took += f", evaluation {attempt['eval_seconds']:.0f} s"
    if rec["type"] not in rules.SCORED:
        kept = ", ".join(f"kept/{k}" for k in rec.get("kept", [])) or "none"
        return f"Written by arl. Not scored. {took}. {where}\nSmall result files: {kept}. Everything else is under artifacts/."
    comp, metrics = rec["comparison"], rec["metrics"]
    ref = ledger.record(comp["reference"]) if comp.get("reference") else None
    head = ["metric", "role", "value"] + ([f"reference {ref['id']}", "gain"] if ref else [])
    rows = []
    for m in cfg.metrics:
        row = [m.name, m.role, fmt(metrics[m.name])]
        if ref:
            before = ref["metrics"].get(m.name)
            row += [fmt(before), f"{m.gain(metrics[m.name], before):+.6g}" if before is not None else ""]
        rows.append(row)
    lines = [f"Written by arl. Outcome: **{comp['outcome']}**.", "", table(head, rows)]
    if ref:
        lines.append(f"A change of at least {fmt(comp['min_delta'])} on {comp['metric']} counts as better or worse.")
    if comp["gate_failures"]:
        lines.append("Constraints not met: " + "; ".join(comp["gate_failures"]) + ".")
    lines.append(f"{took}. {where}")
    return "\n".join(lines)


def _section(body: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}.*?\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    return match.group(1).strip() if match else ""


def review_facts(ledger: Ledger, trigger: str, rs: dict[str, Any]) -> str:
    lock = contract.load(ledger)
    by_id = {e["id"]: e for e in ledger.experiments()}
    window = [by_id[i] for i in rs["window"]]
    out = ["## Facts", "", f"Written by arl on {now()[:10]}. Trigger: {trigger}.", ""]
    out.append(f"Window: {len(window)} experiments judged since {rs['last'] or 'the start'}.")
    out += ["", exp_table(ledger, window), "Tracks:", "", track_table(ledger, lock)]
    out.append(f"Budget: {_budget_line(ledger)}.")
    hyps = [h for h in ledger.hypotheses() if h["status"] == "open"]
    out.append("Open hypotheses: " + ("; ".join(f"{h['id']} {h['title']}" for h in hyps) or "none") + ".")
    ideas = [i for i in ledger.ideas() if i["status"] == "open"]
    out.append("Open ideas: " + ("; ".join(f"{i['id']} {i['title']}" for i in ideas) or "none") + ".")
    first = ledger.cfg.splits[0]
    others = sum(1 for e in by_id.values() for r in e.get("reruns", []) if r["split"] != first)
    if len(ledger.cfg.splits) > 1:
        out.append(f"Runs on a confirmation split so far: {others}.")
    if rs["last"]:
        tripwires = _section(ledger.review(rs["last"])[1], "6. Tripwires")
        out += ["", f"Tripwires set by {rs['last']}:", "", tripwires or "none written"]
    return "\n".join(out)


# --- briefs -----------------------------------------------------------------------

_CHANGE = {
    "baseline": "Change nothing: a baseline measures the track head as it is.",
    "tune": "Edit {exp}/params.json. Params only: the code stays the track head.",
    "feature": (
        "Change the code in {worktree} (it is on branch {branch}), then commit with\n"
        "   `{arl} exp commit {target} -m \"...\"`. Leave {research}/ alone in that worktree."
    ),
    "diagnose": (
        "Add the instrumentation in {worktree} (it is on branch {branch}) and commit with\n"
        "   `{arl} exp commit {target} -m \"...\"`. It must observe, not change behaviour."
    ),
}


def next_text(ledger: Ledger, action: dict[str, Any]) -> str:
    stage = action["stage"]
    ctx: dict[str, Any] = {
        "arl": LAUNCHER,
        "root": ROOT,
        "home": ledger.home,
        "research": ledger.name,
        "target": action.get("target", ""),
        "why": action.get("why", ""),
    }
    if stage in ("judge", "design", "crashed"):
        rec = ledger.record(action["target"])
        ctx["exp"] = ledger.exp_dir(rec["id"])
        ctx["worktree"] = ledger.worktree_path(rec["track"])
        ctx["branch"] = rec["branch"]
        ctx["change_step"] = fill(_CHANGE[rec["type"]], **ctx)
        ctx["outcome"] = rec.get("comparison", {}).get("outcome", "not scored")
        ctx["allowed"] = ", ".join(rules.allowed_verdicts(rec))
        flags = ""
        if rec["type"] in ("tune", "feature"):
            flags = "".join(f" \\\n       --hyp {h}=<supported|refuted|inconclusive|open>" for h in rec["hypotheses"])
        ctx["hyp_flags"] = flags
        if stage == "crashed" and rec.get("attempts"):
            ctx["why"] = rec["attempts"][-1].get("failed", ctx["why"])
    name = stage
    if stage == "judge" and ledger.record(action["target"])["type"] == "diagnose":
        name = "judge_diagnose"
    head = f"NEXT: {stage}" + (f" {action['target']}" if action.get("target") else "")
    return f"{head}\nWhy: {action.get('why', '')}\n\n" + fill(template(f"briefs/{name}.md"), **ctx)
