"""Immutable valuation versions and approvals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..app import App
from ..db.core import insert, one
from ..util import D, dstr, from_json, iso_utc, new_id, stable_hash, to_json
from .dcf import ENGINE_VERSION, ScenarioInputs, run_dcf, sensitivity


@dataclass
class ValuationRecord:
    id: str
    security_id: str
    version_no: int
    created_at: str
    evidence_as_of: str
    bear: Decimal | None
    base: Decimal | None
    bull: Decimal | None
    base_meaningful: bool
    terminal_share: Decimal | None
    approved: bool
    downside_reviewed: bool
    label: str
    outputs: dict
    inputs: dict


def create_valuation(app: App, security_id: str, scenarios: dict[str, ScenarioInputs], *, evidence_as_of: datetime | str,
                     author: str = "ENGINE", label: str = "ACTUAL", change_reason: str = "") -> str:
    if set(scenarios) != {"bear", "base", "bull"}:
        raise ValueError("bear, base and bull scenarios are required")
    cap = app.policy.valuation.terminal_growth_cap
    results = {k: run_dcf(v, cap) for k, v in scenarios.items()}
    outputs = {k: r.to_dict() for k, r in results.items()}
    outputs["sensitivity_base"] = sensitivity(scenarios["base"], cap=cap)
    inputs = {k: v.model_dump(mode="json") for k, v in scenarios.items()}
    prev = one(app.conn, "SELECT id, version_no FROM valuation_version WHERE security_id=? ORDER BY version_no DESC LIMIT 1",
               (security_id,))
    vid = new_id("val")
    insert(app.conn, "valuation_version", {
        "id": vid, "security_id": security_id, "version_no": (prev["version_no"] + 1) if prev else 1,
        "prev_version_id": prev["id"] if prev else None, "model": "FCFF_DCF", "engine_version": ENGINE_VERSION,
        "evidence_as_of": evidence_as_of if isinstance(evidence_as_of, str) else iso_utc(evidence_as_of),
        "inputs_json": to_json(inputs), "outputs_json": to_json(outputs), "inputs_hash": stable_hash(inputs),
        "bear_value_ps": dstr(results["bear"].value_per_share), "base_value_ps": dstr(results["base"].value_per_share),
        "bull_value_ps": dstr(results["bull"].value_per_share), "author": author, "label": label,
        "change_reason": change_reason, "created_at": app.now_iso(),
    })
    app.audit("valuation.created", "valuation_version", vid, {"security_id": security_id})
    return vid


def approve_valuation(app: App, valuation_id: str, *, downside_reviewed: bool, note: str = "", approver: str = "owner") -> None:
    insert(app.conn, "valuation_approval", {"id": new_id("vap"), "valuation_version_id": valuation_id,
                                            "approved_at": app.now_iso(), "approver": approver,
                                            "downside_reviewed": int(downside_reviewed), "note": note})
    app.audit("valuation.approved", "valuation_version", valuation_id, {"downside_reviewed": downside_reviewed})


def _record(app: App, r, as_of: str | None = None) -> ValuationRecord:
    ap = one(app.conn, "SELECT * FROM valuation_approval WHERE valuation_version_id=?", (r["id"],))
    if ap is not None and as_of is not None and ap["approved_at"] > as_of:
        ap = None   # approval happened after the as-of time: not visible to that decision
    outputs = from_json(r["outputs_json"])
    return ValuationRecord(
        id=r["id"], security_id=r["security_id"], version_no=r["version_no"], created_at=r["created_at"],
        evidence_as_of=r["evidence_as_of"], bear=D(r["bear_value_ps"]), base=D(r["base_value_ps"]),
        bull=D(r["bull_value_ps"]), base_meaningful=bool(outputs["base"]["meaningful"]),
        terminal_share=D(outputs["base"]["terminal_share"]), approved=ap is not None,
        downside_reviewed=bool(ap and ap["downside_reviewed"]), label=r["label"], outputs=outputs,
        inputs=from_json(r["inputs_json"]))


def latest_valuation(app: App, security_id: str, as_of: str | None = None, approved_only: bool = False) -> ValuationRecord | None:
    sql = "SELECT v.* FROM valuation_version v"
    if approved_only:
        sql += " JOIN valuation_approval a ON a.valuation_version_id = v.id"
    sql += " WHERE v.security_id=?"
    params: list = [security_id]
    if as_of:
        sql += " AND v.created_at<=?"
        params.append(as_of)
        if approved_only:
            sql += " AND a.approved_at<=?"
            params.append(as_of)
    r = one(app.conn, sql + " ORDER BY v.version_no DESC LIMIT 1", params)
    return _record(app, r, as_of) if r else None


def get_valuation(app: App, valuation_id: str) -> ValuationRecord:
    return _record(app, one(app.conn, "SELECT * FROM valuation_version WHERE id=?", (valuation_id,)))
