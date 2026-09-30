"""Security / issuer registry with stable ids and identifier history.

Stable ids (``sec_...``/``iss_...``) never change; tickers are identifiers with validity
ranges, so a ticker change does not break history. Multiple share classes share one issuer,
which is what issuer-level concentration limits use.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from ..db.core import insert, one, all_rows
from ..util import new_id

# Instruments that bypass company valuation. Extend via ``register_security(..., security_type='ETF')``.
KNOWN_ETFS = {
    "SCHG", "SCHB", "SCHX", "SCHD", "VOO", "VTI", "SPY", "IVV", "QQQ", "VUG", "VTV", "IWF",
    "IWD", "RSP", "VT", "VXUS", "BND", "AGG", "SGOV", "BIL", "SHV", "VLUE", "QUAL", "MTUM",
}


def sic_to_sector(sic: str | int | None) -> str | None:
    """Coarse sector from SIC (division / major group). ``None`` when SIC unknown.

    This is a documented simplification (SIC is dated); override per issuer when wrong.
    """
    if sic in (None, ""):
        return None
    s = int(sic)
    if s < 1000:
        return "Agriculture"
    if s < 1500:
        return "Energy & Mining" if s in range(1300, 1400) or s < 1300 else "Energy & Mining"
    if s < 1800:
        return "Construction"
    if s < 4000:
        mg = s // 100
        if mg in (28,):
            return "Health Care" if s in (2833, 2834, 2835, 2836) else "Materials"
        if mg in (35, 36, 38):
            if s in (3841, 3842, 3843, 3844, 3845, 3851):
                return "Health Care"
            return "Technology"
        if mg in (29,):
            return "Energy & Mining"
        if mg in (20, 21):
            return "Consumer Staples"
        if mg in (22, 23, 25, 31, 39):
            return "Consumer Discretionary"
        if mg == 37:
            return "Industrials" if s in (3720, 3721, 3724, 3728, 3760, 3790) else "Consumer Discretionary"
        if mg in (24, 26, 32, 33, 34):
            return "Materials" if mg in (24, 26, 32, 33) else "Industrials"
        if mg == 27:
            return "Communication Services"
        return "Industrials"
    if s < 5000:
        mg = s // 100
        if mg == 48:
            return "Communication Services"
        if mg == 49:
            return "Utilities"
        return "Industrials"
    if s < 5200:
        return "Industrials"
    if s < 6000:
        return "Consumer Staples" if s in (5400, 5411, 5412, 5912) else "Consumer Discretionary"
    if s < 6800:
        return "Financials"
    if s == 6798:
        return "Real Estate"
    if s < 7000:
        return "Financials"
    if s < 9000:
        mg = s // 100
        if mg == 73:
            return "Technology"
        if mg == 80:
            return "Health Care"
        if mg == 78:
            return "Communication Services"
        return "Consumer Discretionary" if mg in (70, 72, 79) else "Industrials"
    return "Other"


def industry_group(sic: str | int | None) -> str | None:
    if sic in (None, ""):
        return None
    return f"SIC{int(sic) // 100:02d}"


@dataclass
class SecurityRef:
    id: str
    symbol: str
    security_type: str
    issuer_id: str | None
    issuer_name: str | None
    sector: str | None
    cik: str | None


def get_or_create_issuer(conn: sqlite3.Connection, now_iso: str, *, name: str, cik: str | None = None,
                         sic: str | None = None, sic_description: str | None = None,
                         sector: str | None = None, fiscal_year_end: str | None = None) -> str:
    if cik:
        cik = str(cik).zfill(10)
        row = one(conn, "SELECT id FROM issuer WHERE cik = ?", (cik,))
        if row:
            return row["id"]
    iid = new_id("iss")
    insert(conn, "issuer", {
        "id": iid, "name": name, "cik": cik, "sic": sic, "sic_description": sic_description,
        "sector": sector or sic_to_sector(sic), "industry_group": industry_group(sic),
        "fiscal_year_end": fiscal_year_end, "created_at": now_iso,
    })
    return iid


def update_issuer_classification(conn: sqlite3.Connection, issuer_id: str, *, sic: str | None,
                                 sic_description: str | None, fiscal_year_end: str | None,
                                 sector_override: str | None = None) -> None:
    """Classification is reference data (not evidence) so in-place refresh is allowed."""
    conn.execute(
        "UPDATE issuer SET sic = COALESCE(?, sic), sic_description = COALESCE(?, sic_description),"
        " fiscal_year_end = COALESCE(?, fiscal_year_end), industry_group = COALESCE(?, industry_group),"
        " sector = COALESCE(?, sector) WHERE id = ?",
        (sic, sic_description, fiscal_year_end, industry_group(sic), sector_override or sic_to_sector(sic),
         issuer_id),
    )


def find_security(conn: sqlite3.Connection, symbol: str, on: date | None = None) -> str | None:
    symbol = symbol.upper().strip()
    rows = all_rows(conn, "SELECT security_id, valid_from, valid_to FROM security_identifier "
                          "WHERE id_type='TICKER' AND value=?", (symbol,))
    for r in rows:
        if on is None:
            if r["valid_to"] is None:
                return r["security_id"]
            continue
        vf = date.fromisoformat(r["valid_from"]) if r["valid_from"] else date.min
        vt = date.fromisoformat(r["valid_to"]) if r["valid_to"] else date.max
        if vf <= on <= vt:
            return r["security_id"]
    if rows:
        return rows[0]["security_id"]
    return None


def register_security(conn: sqlite3.Connection, now_iso: str, symbol: str, *,
                      security_type: str | None = None, issuer_id: str | None = None,
                      issuer_name: str | None = None, cik: str | None = None, share_class: str | None = None,
                      exchange: str | None = None, source: str = "user") -> str:
    """Return the security id for ``symbol``, creating it (and an issuer) if needed."""
    symbol = symbol.upper().strip()
    existing = find_security(conn, symbol)
    if existing:
        return existing
    if security_type is None:
        security_type = "ETF" if symbol in KNOWN_ETFS else "UNKNOWN"
    if issuer_id is None and security_type not in ("ETF", "FUND"):
        issuer_id = get_or_create_issuer(conn, now_iso, name=issuer_name or symbol, cik=cik)
    sid = new_id("sec")
    insert(conn, "security", {
        "id": sid, "issuer_id": issuer_id, "symbol": symbol, "share_class": share_class,
        "security_type": security_type, "exchange": exchange, "currency": "USD", "created_at": now_iso,
    })
    insert(conn, "security_identifier", {
        "security_id": sid, "id_type": "TICKER", "value": symbol, "valid_from": None, "valid_to": None,
        "source": source, "recorded_at": now_iso,
    })
    return sid


def set_security_type(conn: sqlite3.Connection, security_id: str, security_type: str) -> None:
    conn.execute("UPDATE security SET security_type=? WHERE id=?", (security_type, security_id))


def security_ref(conn: sqlite3.Connection, security_id: str) -> SecurityRef:
    r = one(conn, """SELECT s.id, s.symbol, s.security_type, s.issuer_id, i.name AS issuer_name,
                            i.sector, i.cik
                     FROM security s LEFT JOIN issuer i ON i.id = s.issuer_id WHERE s.id=?""", (security_id,))
    if r is None:
        raise KeyError(security_id)
    return SecurityRef(id=r["id"], symbol=r["symbol"], security_type=r["security_type"],
                       issuer_id=r["issuer_id"], issuer_name=r["issuer_name"], sector=r["sector"], cik=r["cik"])


def resolve(conn: sqlite3.Connection, symbol_or_id: str) -> str:
    if symbol_or_id.startswith("sec_"):
        return symbol_or_id
    sid = find_security(conn, symbol_or_id)
    if sid is None:
        raise KeyError(f"unknown security {symbol_or_id!r}")
    return sid
