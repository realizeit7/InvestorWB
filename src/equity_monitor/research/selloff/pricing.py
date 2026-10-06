"""Prices for selloff events: retrieval with an identity guard, the protocol's decline eligibility, and an outcome
COVERAGE audit. No forward returns are computed in sr-0.1 — only whether data exist to compute them later.

Identity guard: a ticker can be reused by a different company after a delisting, and free sources drop delisted
symbols. A price history is used only if it starts before the event window and the provider's company name matches
the filing's company name; otherwise the event stays visible as IDENTITY_UNVERIFIED or NO_DATA (never assumed).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable

from ...app import App
from ...data import calendar as cal
from ...data.http import ProviderError
from ...data.prices import PriceFetch, YahooChartProvider, actions_for, bar_on, parse_yahoo_chart, store_fetch
from ...data.rawstore import save_raw
from ...db.core import all_rows, insert, one
from ...util import new_id, parse_utc, to_json
from .home import load_protocol, protocol_hash

BENCHMARKS = ("SPY", "XBI")
PROVIDER = "yahoo_chart"
History = Callable[[str, date, date], PriceFetch]          # (symbol, start, end) -> PriceFetch with raw JSON
_STOP = {"inc", "corp", "corporation", "co", "ltd", "plc", "holdings", "holding", "the", "company", "limited", "llc",
         "pharmaceuticals", "pharmaceutical", "therapeutics", "biosciences", "bio", "biopharma", "group", "sa", "nv"}


def make_history() -> History:
    prov = YahooChartProvider()
    prov.client.min_interval_s = 2.0
    return prov.fetch


def _tokens(name: str | None) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t not in _STOP and len(t) > 1}


def _meta(raw: bytes | None) -> dict:
    try:
        m = json.loads(raw)["chart"]["result"][0]["meta"]
    except Exception:  # noqa: BLE001 — absent or malformed meta is recorded as unknown
        return {}
    return {k: m.get(k) for k in ("symbol", "longName", "shortName", "firstTradeDate", "instrumentType", "exchangeName")}


def _security(app: App, symbol: str, issuer_id: str | None, kind: str) -> str:
    """Research securities are keyed TICKER@CIK so a reused ticker can never merge two companies."""
    from ...data.securities import register_security
    if kind == "ETF":
        return register_security(app.conn, app.now_iso(), symbol, security_type="ETF", source="selloff")
    cik = one(app.conn, "SELECT cik FROM issuer WHERE id=?", (issuer_id,))["cik"]
    key = f"{symbol}@{int(cik)}"
    return register_security(app.conn, app.now_iso(), key, security_type="COMMON", issuer_id=issuer_id, source="selloff")


def fetch_benchmarks(app: App, history: History, start: date, end: date) -> dict:
    out = {}
    for sym in BENCHMARKS:
        sid = _security(app, sym, None, "ETF")
        try:
            pf = history(sym, start, end)
        except ProviderError as exc:
            insert(app.conn, "sr_price_check", {"id": new_id("srpc"), "subject": sym, "symbol": sym, "provider": PROVIDER,
                                                "status": "ERROR", "detail_json": to_json({"error": str(exc)[:300]}),
                                                "raw_object_id": None, "checked_at": app.now_iso()})
            out[sym] = "ERROR"
            continue
        rid = save_raw(app, PROVIDER, pf.url, pf.raw, "application/json", "json") if pf.raw else None
        store_fetch(app, sid, pf, PROVIDER)
        insert(app.conn, "sr_price_check", {"id": new_id("srpc"), "subject": sym, "symbol": sym, "provider": PROVIDER,
                                            "status": "OK", "detail_json": to_json({"bars": len(pf.bars),
                                            "first": pf.bars[0].session_date if pf.bars else None,
                                            "last": pf.bars[-1].session_date if pf.bars else None}),
                                            "raw_object_id": rid, "checked_at": app.now_iso()})
        out[sym] = len(pf.bars)
    return out


def fetch_event_prices(app: App, event: dict, history: History, today: date, sub: dict | None = None) -> dict:
    """Fetch the ticker-at-time history and apply the identity guard. If the ticker at the time has no data, the
    SAME CIK's current EDGAR tickers are tried (renamed companies keep their history under the new symbol), still
    requiring a name match with one of the CIK's EDGAR names and a history that starts before the event window.
    Idempotent per event."""
    prev = one(app.conn, "SELECT * FROM sr_price_check WHERE subject=? ORDER BY checked_at DESC, rowid DESC LIMIT 1",
               (event["id"],))
    if prev and prev["status"] in ("OK", "NO_DATA", "IDENTITY_UNVERIFIED"):
        return dict(prev)
    row = _try_symbol(app, event, history, today, event["ticker_at_time"], [], "TICKER_AT_TIME")
    if row["status"] == "NO_DATA" and sub:
        names = [sub.get("name")] + list(sub.get("former_names_list") or [])
        for alt in [t for t in sub.get("current_tickers") or [] if t != event["ticker_at_time"]]:
            alt_row = _try_symbol(app, event, history, today, alt, names, "CURRENT_TICKER_SAME_CIK", record=False)
            if alt_row["status"] == "OK":
                d = json.loads(alt_row["detail_json"])
                d["ticker_at_time_result"] = json.loads(row["detail_json"]).get("reason")
                alt_row["detail_json"] = to_json(d)
                row = alt_row
                break
    insert(app.conn, "sr_price_check", row)
    return row


def _try_symbol(app: App, event: dict, history: History, today: date, sym: str | None, extra_names: list,
                source: str, record: bool = True) -> dict:
    detail: dict = {"ticker_at_time": event["ticker_at_time"], "symbol_used": sym, "symbol_source": source}
    status, rid = None, None
    if not sym:
        status, detail["reason"] = "NO_DATA", "ticker at the time unknown"
    else:
        pre = date.fromisoformat(event["pre_session"])
        # always through TODAY: the provider's prices are adjusted for every split up to today, and only splits
        # inside the requested range can be reversed — a shorter window leaves later (reverse) splits baked in
        start, end = pre - timedelta(days=420), today
        try:
            pf = history(sym, start, end)
        except ProviderError as exc:
            status, detail["reason"] = "NO_DATA", str(exc)[:300]
            pf = None
        if pf is not None:
            rid = save_raw(app, PROVIDER, pf.url, pf.raw, "application/json", "json") if pf.raw else None
            meta = _meta(pf.raw)
            detail |= {"provider_name": meta.get("longName") or meta.get("shortName"), "meta": meta,
                       "bars": len(pf.bars), "first_bar": pf.bars[0].session_date.isoformat() if pf.bars else None,
                       "last_bar": pf.bars[-1].session_date.isoformat() if pf.bars else None}
            filing_name = event["company_name_at_time"] or one(app.conn, "SELECT name FROM issuer WHERE id=?",
                                                               (event["issuer_id"],))["name"]
            overlap = set()
            for nm in [filing_name] + [n for n in extra_names if n]:
                overlap |= _tokens(nm) & _tokens(detail["provider_name"])
            detail["name_overlap"] = sorted(overlap)
            if not pf.bars:
                status, detail["reason"] = "NO_DATA", "no bars returned"
            elif pf.bars[0].session_date > pre:
                status = "IDENTITY_UNVERIFIED"
                detail["reason"] = "history starts after the event window: the ticker may now belong to another listing"
            elif not overlap:
                status = "IDENTITY_UNVERIFIED"
                detail["reason"] = f"provider name {detail['provider_name']!r} does not match filing name {filing_name!r}"
            else:
                status = "OK"
                sid = _security(app, sym, event["issuer_id"], "COMMON")
                store_fetch(app, sid, pf, PROVIDER)
                detail["security_id"] = sid
    return {"id": new_id("srpc"), "subject": event["id"], "symbol": sym or "?", "provider": PROVIDER, "status": status,
            "detail_json": to_json(detail), "raw_object_id": rid, "checked_at": app.now_iso()}


def _sid(app: App, event: dict) -> str | None:
    r = one(app.conn, "SELECT detail_json FROM sr_price_check WHERE subject=? AND status='OK' ORDER BY checked_at DESC LIMIT 1",
            (event["id"],))
    return json.loads(r["detail_json"]).get("security_id") if r else None


def _close_on_or_before(app: App, sid: str, d: date) -> tuple[date, Decimal] | None:
    r = one(app.conn, "SELECT session_date, close FROM price_bar WHERE security_id=? AND session_date<=? "
                      "ORDER BY session_date DESC, provider LIMIT 1", (sid, d.isoformat()))
    return (date.fromisoformat(r["session_date"]), Decimal(r["close"])) if r else None


def _close_on_or_after(app: App, sid: str, d: date) -> tuple[date, Decimal] | None:
    r = one(app.conn, "SELECT session_date, close FROM price_bar WHERE security_id=? AND session_date>=? "
                      "ORDER BY session_date, provider LIMIT 1", (sid, d.isoformat()))
    return (date.fromisoformat(r["session_date"]), Decimal(r["close"])) if r else None


def _window_move(app: App, sid: str | None, pre: date, meas: date) -> dict:
    if sid is None:
        return {"status": "NO_DATA"}
    a, b = _close_on_or_before(app, sid, pre), _close_on_or_after(app, sid, meas)
    if a is None or b is None:
        return {"status": "NO_DATA"}
    notes = []
    if a[0] != pre:
        notes.append(f"no bar on {pre}; used {a[0]}")
    if b[0] != meas:
        notes.append(f"no bar on {meas} (halt or data gap); used {b[0]}")
    if (b[0] - meas).days > 7:
        return {"status": "NO_DATA", "notes": notes + ["first bar after the measurement session is > 7 days later"]}
    splits = actions_for(app, sid, a[0] + timedelta(days=1), b[0], "SPLIT")
    if splits:
        notes.append("split inside the window")
    return {"status": "OK", "pre": [a[0].isoformat(), str(a[1])], "post": [b[0].isoformat(), str(b[1])],
            "move": str((b[1] / a[1] - 1).quantize(Decimal("0.0001"))), "notes": notes}


def eligibility(app: App, event: dict) -> dict:
    """Protocol §7: decline <= threshold, pre-event market cap and price floors. UNDETERMINED when data are missing."""
    from ..fundamentals import FactView
    proto, _h, _t = load_protocol()
    pdl = proto["price_decline"]
    pre, meas = date.fromisoformat(event["pre_session"]), date.fromisoformat(event["measurement_session"])
    sid = _sid(app, event)
    mv = _window_move(app, sid, pre, meas)
    bm = {s: _window_move(app, one(app.conn, "SELECT id FROM security WHERE symbol=?", (s,))["id"]
                          if one(app.conn, "SELECT id FROM security WHERE symbol=?", (s,)) else None, pre, meas)
          for s in BENCHMARKS}
    detail = {"window": [pre.isoformat(), meas.isoformat()], "event": mv,
              "benchmarks": {s: v.get("move") for s, v in bm.items()}}
    reasons, undetermined = [], []
    if mv["status"] != "OK":
        undetermined.append("no usable event-window prices (see price check)")
    else:
        dec = Decimal(mv["move"])
        detail["decline"] = str(dec)
        for s, v in bm.items():
            if v.get("move") is not None:
                detail[f"relative_to_{s}"] = str(dec - Decimal(v["move"]))
        if dec > Decimal(pdl["threshold"]):
            reasons.append(f"decline {dec:.1%} is smaller than the {Decimal(pdl['threshold']):.0%} threshold")
        pre_close = Decimal(mv["pre"][1])
        if pre_close < Decimal(pdl["min_pre_event_price_usd"]):
            reasons.append(f"pre-event price {pre_close} below ${pdl['min_pre_event_price_usd']}")
        fv = FactView(app, event["issuer_id"], parse_utc(event["public_earliest"]))
        sh = fv.instant("shares_outstanding", on_or_before=pre)
        from .records import STALE_DAYS
        if sh is not None and (pre - sh.end).days > STALE_DAYS:
            undetermined.append(f"shares outstanding stale (period {sh.end}); market cap undetermined")
        elif sh is None:
            undetermined.append("point-in-time shares outstanding unknown (market cap undetermined)")
        else:
            cap = sh.value * pre_close
            detail["pre_event_market_cap"] = str(cap.quantize(Decimal("1")))
            detail["shares_source"] = f"{sh.public_at} {sh.accession} (period {sh.end})"
            if cap < Decimal(pdl["min_pre_event_market_cap_usd"]):
                reasons.append(f"pre-event market cap {cap:,.0f} below {Decimal(pdl['min_pre_event_market_cap_usd']):,.0f}")
    status = "INELIGIBLE" if reasons else "UNDETERMINED" if undetermined else "ELIGIBLE"
    detail["reasons"], detail["undetermined"] = reasons, undetermined
    insert(app.conn, "sr_eligibility", {"id": new_id("srel"), "event_id": event["id"], "status": status,
                                        "detail_json": to_json(detail), "protocol_hash": protocol_hash(app),
                                        "computed_at": app.now_iso()})
    return {"status": status, **detail}


# ------------------------------------------------------------------ outcome coverage (availability only)
# DEFM14A (merger proxy) and SC 14D9 (target's response to a tender offer) are filed by the company being acquired;
# SC TO-T and S-4 can be filed by a BIDDER, so they are recorded as acquisition-related with an unknown role
STATUS_FORMS = {"25": "DELISTING", "25-NSE": "DELISTING", "15-12B": "DEREGISTRATION", "15-12G": "DEREGISTRATION",
                "15-15D": "DEREGISTRATION", "DEFM14A": "ACQUISITION", "SC 14D9": "ACQUISITION",
                "SC TO-T": "ACQUISITION_RELATED_ROLE_UNKNOWN", "S-4": "ACQUISITION_RELATED_ROLE_UNKNOWN"}
STATUS_ITEMS = {"1.03": "BANKRUPTCY", "2.01": "ACQUISITION_OR_DISPOSITION", "3.01": "LISTING_NOTICE"}


def add_sessions(d: date, n: int) -> date:
    for _ in range(n):
        d = cal.next_session(d)
    return d


def _availability(app: App, sid: str | None, entry: date, horizons: list[int], today: date) -> dict:
    if sid is None:
        return {"entry_open": False, "horizons": {str(h): "NO_PRICE_SOURCE" for h in horizons}}
    out = {"entry_open": bool((b := bar_on(app, sid, entry)) and b.get("open")), "horizons": {}}
    for h in horizons:
        tgt = add_sessions(entry, h)
        if tgt > today:
            out["horizons"][str(h)] = "NOT_MATURED"
            continue
        sessions = cal.sessions_between(entry, tgt)
        have = {r["session_date"] for r in all_rows(app.conn, "SELECT DISTINCT session_date FROM price_bar WHERE "
                                                              "security_id=? AND session_date BETWEEN ? AND ?",
                                                    (sid, entry.isoformat(), tgt.isoformat()))}
        missing = [s for s in sessions if s.isoformat() not in have]
        tgt_bar = bar_on(app, sid, tgt)
        out["horizons"][str(h)] = ("AVAILABLE" if tgt_bar and tgt_bar.get("open") and not missing else
                                   "AVAILABLE_WITH_GAPS" if tgt_bar and tgt_bar.get("open") else "NO_PRICE_AT_HORIZON")
        out[f"missing_sessions_{h}"] = len(missing)
    last = one(app.conn, "SELECT MAX(session_date) AS d FROM price_bar WHERE security_id=?", (sid,))["d"]
    out["last_bar"] = last
    acts = actions_for(app, sid, entry, add_sessions(entry, max(horizons)))
    out["corporate_actions"] = sorted({a["action_type"] for a in acts})
    return out


def outcome_audit(app: App, event: dict, sub: dict | None, today: date) -> dict:
    """Whether a later evaluation could be supported — never the outcome itself."""
    proto, _h, _t = load_protocol()
    horizons = proto["outcomes_audit"]["horizons_sessions"]
    entry = date.fromisoformat(event["entry_session"])
    pc = one(app.conn, "SELECT status, detail_json FROM sr_price_check WHERE subject=? ORDER BY checked_at DESC LIMIT 1",
             (event["id"],))
    detail: dict = {"price_check": pc["status"] if pc else "NOT_RUN",
                    "stock": _availability(app, _sid(app, event), entry, horizons, today),
                    "benchmarks": {s: _availability(app, (one(app.conn, "SELECT id FROM security WHERE symbol=?", (s,)) or
                                                          {"id": None})["id"], entry, horizons, today)
                                   for s in BENCHMARKS}}
    evidence = []
    if sub is not None:
        cut = event["decision_cutoff"]
        for f in sub["filings"]:
            acc_t = f.get("acceptanceDateTime") or ""
            if not acc_t or acc_t.replace(".000Z", "Z") <= cut.replace("+00:00", "Z"):
                continue
            if f["form"] in STATUS_FORMS:
                evidence.append({"form": f["form"], "date": f["filingDate"], "meaning": STATUS_FORMS[f["form"]],
                                 "accession": f["accessionNumber"]})
            for it in (f.get("items") or "").split(","):
                if f["form"].startswith("8-K") and it.strip() in STATUS_ITEMS:
                    evidence.append({"form": f"8-K item {it.strip()}", "date": f["filingDate"],
                                     "meaning": STATUS_ITEMS[it.strip()], "accession": f["accessionNumber"]})
        latest = max((f["filingDate"] for f in sub["filings"]), default=None)
        detail["latest_filing"] = latest
        detail["current_tickers"] = sub.get("current_tickers")
        detail["former_names"] = [n.get("name") for n in sub.get("former_names") or []]
    detail["status_evidence"] = sorted(evidence, key=lambda e: e["date"])[:20]
    kinds = {e["meaning"] for e in evidence}
    latest = date.fromisoformat(detail["latest_filing"]) if detail.get("latest_filing") else None
    # 8-K item 2.01 can be the company buying something, so it is evidence to read, not a status by itself
    detail["status_summary"] = ("ACQUISITION_EVIDENCE" if "ACQUISITION" in kinds else
                                "BANKRUPTCY_EVIDENCE" if "BANKRUPTCY" in kinds else
                                "DELISTING_OR_DEREGISTRATION_EVIDENCE" if kinds & {"DELISTING", "DEREGISTRATION"} else
                                "ACQUISITION_RELATED_FILINGS_ROLE_UNKNOWN" if "ACQUISITION_RELATED_ROLE_UNKNOWN" in kinds else
                                "UNKNOWN" if sub is None or latest is None else
                                "FILINGS_STOPPED_REASON_UNDETERMINED" if (today - latest).days > 400 else "STILL_FILING")
    insert(app.conn, "sr_outcome_audit", {"id": new_id("srout"), "event_id": event["id"], "detail_json": to_json(detail),
                                          "computed_at": app.now_iso()})
    return detail
