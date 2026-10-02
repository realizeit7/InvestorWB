"""Conservative monthly LLM spending control (paid providers only).

What it guarantees, given the pricing table is right:
- No paid request is sent without a configured ``llm.monthly_budget_usd`` and a known price for the model
  (``PRICING`` or the explicit ``llm.price_*_per_mtok`` overrides). Unknown pricing stops the call.
- Before sending, a worst-case allowance is RESERVED inside an IMMEDIATE SQLite transaction, so two processes
  cannot both spend the same remaining budget: input tokens are bounded by the request's UTF-8 byte count (a
  token is at least one byte), output tokens by ``max_output_tokens`` (which also caps thinking), priced at the
  highest known model price when server-side fallbacks are enabled, and doubled in that case because a refused
  first attempt may also be billed.
- After the response the reservation is SETTLED to the actual cost from reported usage; if usage or the served
  model's price is unknown, or the call raised, the full reservation stays charged.
- Spend this month = settled amounts + open reservations (+ any legacy cost records). Unknown legacy amounts block
  further paid calls until reconciled.

Remaining overshoot (not a guarantee beyond these): a wrong or outdated pricing table, provider charges outside
token usage (e.g. paid server tools, long-context surcharges), or billing for retries the SDK performs internally.
It is therefore a conservative pre-authorization limit, not a provider-enforced hard cap; set a spending limit in
the provider console as well.
"""

from __future__ import annotations

import json
from decimal import ROUND_UP, Decimal

from ..app import App
from ..db.core import all_rows, insert, one
from ..util import dstr, new_id
from .base import PRICING, LLMRequest, LLMResponse, LLMUnavailable

FREE_PROVIDERS = ("none", "fixture")
SUBSCRIPTION_PROVIDERS = ("claude_code",)       # billed by the owner's plan, not per token: capped by calls per day


class BudgetExceeded(LLMUnavailable):
    pass


def _month(app: App) -> str:
    return app.now().strftime("%Y-%m")


def price_for(app: App, model: str) -> tuple[Decimal, Decimal] | None:
    s = app.settings.llm
    if s.price_input_per_mtok is not None and s.price_output_per_mtok is not None:
        return s.price_input_per_mtok, s.price_output_per_mtok
    return PRICING.get(model)


def committed(app: App, month: str | None = None) -> tuple[Decimal, int]:
    """(committed USD this month, number of legacy LLM cost records with UNKNOWN amount)."""
    month = month or _month(app)
    rows = all_rows(app.conn, "SELECT id, kind, reservation_id, amount_usd FROM llm_budget_entry WHERE month=?", (month,))
    settled = {r["reservation_id"] for r in rows if r["kind"] == "SETTLE"}
    total = sum((Decimal(r["amount_usd"]) for r in rows if r["kind"] == "SETTLE"), Decimal(0)) + \
        sum((Decimal(r["amount_usd"]) for r in rows if r["kind"] == "RESERVE" and r["id"] not in settled), Decimal(0))
    # legacy cost records written before this ledger existed (not linked to any ledger entry)
    legacy = all_rows(app.conn, "SELECT amount_usd FROM cost_record WHERE category='LLM' AND substr(occurred_at,1,7)=? "
                                "AND provider NOT IN ('none','fixture','claude_code') AND (ref_id IS NULL OR ref_id NOT IN "
                                "(SELECT llm_call_id FROM llm_budget_entry WHERE llm_call_id IS NOT NULL))", (month,))
    total += sum((Decimal(r["amount_usd"]) for r in legacy if r["amount_usd"] is not None), Decimal(0))
    return total, sum(1 for r in legacy if r["amount_usd"] is None)


def allowance(app: App, provider_name: str, model: str, req: LLMRequest) -> tuple[Decimal, str]:
    s = app.settings.llm
    price = price_for(app, model)
    if price is None:
        raise BudgetExceeded(f"no price is known for model '{model}': refusing a paid call with unknown cost "
                             "(add it to PRICING or set llm.price_input_per_mtok / llm.price_output_per_mtok)")
    fallbacks = provider_name == "anthropic" and s.use_server_fallbacks
    if fallbacks:
        price = (max([price[0]] + [p[0] for p in PRICING.values()]), max([price[1]] + [p[1] for p in PRICING.values()]))
    in_tok = len((req.system + req.user + json.dumps(req.json_schema)).encode("utf-8")) + 64
    out_tok = req.max_output_tokens
    amt = (Decimal(in_tok) * price[0] + Decimal(out_tok) * price[1]) / Decimal(1_000_000)
    if fallbacks:
        amt *= 2
    amt = amt.quantize(Decimal("0.000001"), rounding=ROUND_UP)
    return amt, (f"<= {in_tok} input tokens (bytes) x {price[0]}/M + {out_tok} output tokens x {price[1]}/M"
                 + (" at the highest known price, x2 for a possible fallback attempt" if fallbacks else ""))


def reserve(app: App, provider_name: str, model: str, req: LLMRequest) -> str | None:
    """Reserve the worst-case cost of one request, atomically against concurrent reservations. Returns the
    reservation id, or None for free providers. Raises BudgetExceeded instead of sending."""
    if provider_name in FREE_PROVIDERS:
        return None
    if provider_name in SUBSCRIPTION_PROVIDERS:
        return _reserve_slot(app, provider_name)
    budget = app.settings.llm.monthly_budget_usd
    if budget is None:
        raise BudgetExceeded("paid LLM calls need llm.monthly_budget_usd (no unlimited spending)")
    amt, basis = allowance(app, provider_name, model, req)
    conn = app.conn
    _begin_exclusive(conn)
    try:
        spent, unknown = committed(app)
        if unknown:
            raise BudgetExceeded(f"{unknown} earlier LLM cost record(s) this month have UNKNOWN cost; reconcile them "
                                 "before further paid calls")
        if spent + amt > budget:
            raise BudgetExceeded(f"monthly LLM budget ${budget}: committed ${spent:.4f} + this request's worst case "
                                 f"${amt:.4f} would exceed it ({basis})")
        rid = new_id("llmr")
        insert(conn, "llm_budget_entry", {"id": rid, "month": _month(app), "kind": "RESERVE", "reservation_id": None,
                                          "amount_usd": dstr(amt), "basis": basis, "llm_call_id": None,
                                          "created_at": app.now_iso(), "provider": provider_name})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return rid


def _begin_exclusive(conn) -> None:
    if conn.in_transaction:
        raise BudgetExceeded("budget reservation must not run inside another transaction (it needs an exclusive "
                             "write lock to be safe against concurrent calls)")
    conn.execute("BEGIN IMMEDIATE")                   # serialize reservations across processes


def _reserve_slot(app: App, provider_name: str) -> str:
    """Subscription providers: reserve one of today's call slots BEFORE launching, atomically (a slot is used even if
    the call then fails, so concurrent or crashed calls can never exceed the cap)."""
    cap = app.settings.llm.max_subscription_calls_per_day
    today = app.now().strftime("%Y-%m-%d")
    conn = app.conn
    _begin_exclusive(conn)
    try:
        n = one(conn, "SELECT COUNT(*) AS n FROM llm_budget_entry WHERE kind='RESERVE' AND provider=? AND "
                      "substr(created_at,1,10)=?", (provider_name, today))["n"]
        if n >= cap:
            raise BudgetExceeded(f"{provider_name}: {n} call slots reserved today (UTC) reached "
                                 f"llm.max_subscription_calls_per_day={cap} (your Claude plan's own usage limits also apply)")
        rid = new_id("llmr")
        insert(conn, "llm_budget_entry", {"id": rid, "month": _month(app), "kind": "RESERVE", "reservation_id": None,
                                          "amount_usd": "0", "basis": f"{provider_name} call slot {n + 1}/{cap} (plan usage, "
                                          "not per-token billing)", "llm_call_id": None, "created_at": app.now_iso(),
                                          "provider": provider_name})
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return rid


def settle(app: App, reservation_id: str | None, resp: LLMResponse | None, llm_call_id: str | None) -> Decimal | None:
    """Charge the actual cost (known usage and price), else the full reservation. Returns the charged amount."""
    if reservation_id is None:
        return None
    res = one(app.conn, "SELECT * FROM llm_budget_entry WHERE id=?", (reservation_id,))
    if res["provider"] in SUBSCRIPTION_PROVIDERS:
        insert(app.conn, "llm_budget_entry", {"id": new_id("llms"), "month": res["month"], "kind": "SETTLE",
                                              "reservation_id": reservation_id, "amount_usd": "0",
                                              "basis": "subscription call (plan usage; no per-token charge recorded)",
                                              "llm_call_id": llm_call_id, "created_at": app.now_iso(),
                                              "provider": res["provider"]})
        return Decimal(0)
    actual, basis = None, "usage or price unknown: full reservation charged"
    if resp is not None and resp.input_tokens is not None and resp.output_tokens is not None:
        price = price_for(app, resp.model)
        if price is not None:
            actual = (Decimal(resp.input_tokens) * price[0] + Decimal(resp.output_tokens) * price[1]) / Decimal(1_000_000)
            basis = f"actual usage {resp.input_tokens} in / {resp.output_tokens} out on {resp.model}"
    if resp is None:
        basis = "request raised: outcome unknown, full reservation charged"
    amount = actual if actual is not None else Decimal(res["amount_usd"])
    insert(app.conn, "llm_budget_entry", {"id": new_id("llms"), "month": res["month"], "kind": "SETTLE",
                                          "reservation_id": reservation_id, "amount_usd": dstr(amount), "basis": basis,
                                          "llm_call_id": llm_call_id, "created_at": app.now_iso(),
                                          "provider": res["provider"]})
    return amount
