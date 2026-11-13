"""The mechanical decisions: how a result compares, which verdicts it allows,
when a review is due, when the budget is spent, and what comes next."""

from __future__ import annotations

from typing import Any

from . import contract
from .config import Config
from .ledger import Ledger
from .runner import alive
from .util import now, parse_time

SCORED = ("baseline", "tune", "feature")
TYPES = (*SCORED, "diagnose")
IN_FLIGHT = ("draft", "running", "evaluated", "crashed")


# --- comparing a result -------------------------------------------------------------


def gate_failures(cfg: Config, metrics: dict[str, float], reference: dict[str, float] | None) -> list[str]:
    fails = []
    for m in cfg.metrics:
        if m.role != "constraint":
            continue
        value = metrics[m.name]
        if m.limit is not None and not m.meets(value, m.limit):
            sign = ">=" if m.goal == "max" else "<="
            fails.append(f"{m.name} = {value:.6g}, must be {sign} {m.limit:.6g}")
        if m.max_regression is not None and reference is not None and m.name in reference:
            loss = -m.gain(value, reference[m.name])
            if loss > m.max_regression:
                fails.append(
                    f"{m.name} = {value:.6g} is {loss:.6g} worse than the reference "
                    f"({reference[m.name]:.6g}), {m.max_regression:.6g} allowed"
                )
    return fails


def compare(cfg: Config, metrics: dict[str, float], reference: dict[str, Any] | None) -> dict[str, Any]:
    """Outcome of a scored run against the track's reference experiment.

    reference: improve, neutral, worse or gate_fail. No reference: this run is one.
    """
    primary = cfg.primary
    value = metrics[primary.name]
    ref_metrics = reference["metrics"] if reference else None
    fails = gate_failures(cfg, metrics, ref_metrics)
    out: dict[str, Any] = {
        "metric": primary.name,
        "value": value,
        "gate_failures": fails,
        "target_met": bool(primary.target is not None and not fails and primary.meets(value, primary.target)),
    }
    if reference is None:
        out["outcome"] = "reference"
        return out
    assert ref_metrics is not None
    gain = primary.gain(value, ref_metrics[primary.name])
    out.update(
        reference=reference["id"],
        reference_value=ref_metrics[primary.name],
        gain=gain,
        min_delta=primary.min_delta,
    )
    ref_failed = bool(reference.get("comparison", {}).get("gate_failures"))
    if fails:
        out["outcome"] = "gate_fail"
    elif ref_failed:
        out["outcome"] = "improve"  # inside the constraints beats outside them
    elif gain > 0 and gain >= primary.min_delta:
        out["outcome"] = "improve"
    elif gain < 0 and -gain >= primary.min_delta:
        out["outcome"] = "worse"
    else:
        out["outcome"] = "neutral"
    return out


def allowed_verdicts(rec: dict[str, Any]) -> list[str]:
    """What the agent may decide. The first entry is the default."""
    if rec["status"] == "crashed":
        return ["crash"]
    if rec["type"] == "diagnose":
        return ["finding"]
    outcome = rec.get("comparison", {}).get("outcome")
    if outcome == "reference":
        return ["keep", "discard"]
    if outcome == "improve":
        return ["keep", "discard"]
    if outcome == "neutral":
        return ["discard", "keep"]  # keep only as a simplification, with a reason
    return ["discard"]


# --- tracks -----------------------------------------------------------------------


def reference_for(ledger: Ledger, track: dict[str, Any], eval_digest: str) -> dict[str, Any] | None:
    """The experiment that measured the track head under the current evaluator."""
    eid = track.get("head_experiment")
    if not eid:
        return None
    rec = ledger.record(eid)
    if rec.get("contract", {}).get("eval_digest") != eval_digest:
        return None
    return rec


def in_flight(ledger: Ledger, track: str | None = None) -> list[dict[str, Any]]:
    return [
        e
        for e in ledger.experiments()
        if e["status"] in IN_FLIGHT and (track is None or e["track"] == track)
    ]


def kept(rec: dict[str, Any]) -> bool:
    return rec["status"] == "judged" and rec["verdict"]["decision"] == "keep"


# --- budget -----------------------------------------------------------------------


def budget(ledger: Ledger) -> dict[str, Any]:
    cfg = ledger.cfg
    exps = ledger.experiments()
    ran = [e for e in exps if e.get("attempts")]
    runs = [a for e in exps for a in (*e.get("attempts", []), *e.get("reruns", []))]
    seconds = sum(a.get("run_seconds", 0) + a.get("eval_seconds", 0) for a in runs)
    hours = seconds / 3600
    reasons = []
    if cfg.max_experiments is not None and len(ran) >= cfg.max_experiments:
        reasons.append(f"{len(ran)} experiments run, the budget is {cfg.max_experiments}")
    if cfg.max_run_hours is not None and hours >= cfg.max_run_hours:
        reasons.append(f"{hours:.1f} run hours used, the budget is {cfg.max_run_hours}")
    return {
        "experiments": len(ran),
        "max_experiments": cfg.max_experiments,
        "hours": round(hours, 2),
        "max_run_hours": cfg.max_run_hours,
        "spent": reasons,
    }


def target_met(ledger: Ledger) -> dict[str, Any] | None:
    lock = contract.load(ledger)
    if lock is None or ledger.cfg.primary.target is None:
        return None
    for e in ledger.experiments():
        same = e.get("contract", {}).get("eval_digest") == lock["eval_digest"]
        if kept(e) and same and e.get("comparison", {}).get("target_met"):
            return e
    return None


# --- reviews ----------------------------------------------------------------------


def review_state(ledger: Ledger) -> dict[str, Any]:
    """Whether the periodic reconsideration is due, and why."""
    cfg = ledger.cfg
    reviews = ledger.reviews()
    waiting = next((r for r in reviews if r["status"] == "waiting"), None)
    opened = next((r for r in reviews if r["status"] == "open"), None)
    closed = [r for r in reviews if r["status"] == "closed"]
    last = closed[-1] if closed else None
    since = parse_time(last["closed"]) if last else None

    exps = ledger.experiments()
    judged = [e for e in exps if e["status"] == "judged"]
    recent = [e for e in judged if since is None or parse_time(e["verdict"]["at"]) > since]
    recent.sort(key=lambda e: e["verdict"]["at"])

    reasons = []
    if waiting:
        reasons.append(f"{waiting['id']} waits for a person to decide")
    if opened:
        reasons.append(f"{opened['id']} is open")
    if len(recent) >= cfg.review_every:
        start = last["id"] if last else "the start"
        reasons.append(f"{len(recent)} experiments judged since {start}, a review is due every {cfg.review_every}")
    stagnant = {}
    for track in ledger.tracks():
        if track["status"] != "active":
            continue
        run = 0
        for e in recent:
            if e["track"] != track["name"] or e["type"] not in ("tune", "feature"):
                continue
            run = 0 if e["verdict"]["decision"] == "keep" else run + 1
        stagnant[track["name"]] = run
        if run >= cfg.review_stagnation:
            reasons.append(f"track {track['name']}: {run} scored experiments in a row without a keep")
    if cfg.review_max_days is not None and recent:
        anchor = since or parse_time(min(e["created"] for e in exps))
        days = (parse_time(now()) - anchor).total_seconds() / 86400
        if days > cfg.review_max_days:
            reasons.append(f"{days:.1f} days since the last review, the limit is {cfg.review_max_days}")
    return {
        "due": bool(reasons),
        "reasons": reasons,
        "open": opened["id"] if opened else None,
        "waiting": waiting["id"] if waiting else None,
        "last": last["id"] if last else None,
        "last_decision": last.get("decision") if last else None,
        "judged_since": len(recent),
        "window": [e["id"] for e in recent],
        "stagnant": stagnant,
    }


# --- what comes next --------------------------------------------------------------


def next_action(ledger: Ledger) -> dict[str, Any]:
    """The one thing to do now. Order matters: unfinished work first, then gates."""
    if not ledger.exists():
        return {"stage": "setup", "why": f"no {ledger.name}/arl.toml in {ledger.home}"}
    bad = contract.problems(ledger)
    if bad:
        stage = "freeze" if contract.load(ledger) is None else "contract"
        return {"stage": stage, "why": "; ".join(bad), "stop": stage == "contract"}
    lock = contract.load(ledger)
    assert lock is not None
    rs = review_state(ledger)
    if rs["waiting"]:
        return {"stage": "wait", "target": rs["waiting"], "why": rs["reasons"][0], "stop": True}

    exps = ledger.experiments()
    for e in exps:
        if e["status"] == "running":
            pid = e.get("pid")
            if alive(pid):
                return {"stage": "running", "target": e["id"], "why": f"{e['id']} is running (pid {pid})"}
            return {"stage": "interrupted", "target": e["id"], "why": f"the run of {e['id']} died without a result"}
    for e in exps:
        if e["status"] == "evaluated":
            return {"stage": "judge", "target": e["id"], "why": f"{e['id']} has a result and no verdict"}
    for e in exps:
        if e["status"] == "crashed":
            return {"stage": "crashed", "target": e["id"], "why": f"{e['id']} failed {len(e.get('attempts', []))} time(s)"}
    if rs["open"]:
        return {"stage": "review", "target": rs["open"], "why": f"{rs['open']} is open"}
    for e in exps:
        if e["status"] == "draft":
            return {"stage": "design", "target": e["id"], "why": f"{e['id']} is drafted and has not run"}

    if rs["last_decision"] == "stop":
        return {"stage": "stopped", "target": rs["last"], "why": f"{rs['last']} decided to stop", "stop": True}
    hit = target_met(ledger)
    if hit:
        return {"stage": "target", "target": hit["id"], "why": f"{hit['id']} reached the target", "stop": True}
    spent = budget(ledger)["spent"]
    if spent:
        return {"stage": "budget", "why": "; ".join(spent), "stop": True}
    if rs["due"]:
        return {"stage": "review", "why": "; ".join(rs["reasons"])}

    active = [t for t in ledger.tracks() if t["status"] == "active"]
    if not active:
        return {"stage": "track", "why": "no active track"}
    for t in active:
        if reference_for(ledger, t, lock["eval_digest"]) is None:
            return {
                "stage": "baseline",
                "target": t["name"],
                "why": f"track {t['name']} has no reference under contract v{lock['version']}",
            }
    return {"stage": "hypothesize", "why": "nothing in flight, no gate closed"}
