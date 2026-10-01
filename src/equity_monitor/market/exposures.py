"""Versioned company exposure profiles: benchmarks + economic sensitivities with evidence.

Factor sign convention: each factor has a defined "rising" meaning (below). An exposure's direction says
whether the company is helped (POSITIVE) or hurt (NEGATIVE) when the factor RISES. MIXED/UNKNOWN
directions never produce automatic effects; they route observations to research.

Every exposure needs cited evidence (verified like thesis claims) or ``basis: ANALYST_ASSUMPTION``.
Profiles are immutable versions; only an approved version is used for decisions.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..app import App
from ..db.core import all_rows, insert, one
from ..research.evidence import VERIFIER_VERSION, Citation, ClaimIn, verify_claim
from ..research.fundamentals import FactView, cash_total, debt_total
from ..util import from_json, iso_utc, new_id, stable_hash, to_json
from .series import REFERENCE_ETFS, SECTOR_ETFS

FACTOR_RISING = {
    "RATES": "government yields rise",
    "REFINANCING": "the company's cost of refinancing debt rises",
    "CREDIT_CONDITIONS": "credit spreads widen / lending tightens",
    "CONSUMER_SPENDING": "consumer spending strengthens",
    "ENTERPRISE_SPENDING": "business investment/IT/industrial spending strengthens",
    "INFLATION_INPUT_COSTS": "input costs / inflation rise",
    "EMPLOYMENT": "the labour market strengthens",
    "COMMODITY_OIL": "oil prices rise",
    "COMMODITY_COPPER": "copper prices rise",
    "FX_USD": "the US dollar strengthens",
    "GEOGRAPHY": "conditions in a named region deteriorate",
    "REGULATION": "regulatory pressure increases",
    "CUSTOMER_CONCENTRATION": "a major customer weakens or leaves",
    "SUPPLIER_CONCENTRATION": "a critical supplier is disrupted",
}
Factor = Literal["RATES", "REFINANCING", "CREDIT_CONDITIONS", "CONSUMER_SPENDING", "ENTERPRISE_SPENDING",
                 "INFLATION_INPUT_COSTS", "EMPLOYMENT", "COMMODITY_OIL", "COMMODITY_COPPER", "FX_USD", "GEOGRAPHY",
                 "REGULATION", "CUSTOMER_CONCENTRATION", "SUPPLIER_CONCENTRATION"]

# default market indicators that bear on each factor (series keys / price symbols)
FACTOR_INDICATORS: dict[str, list[str]] = {
    "RATES": ["fred:DGS10", "fred:DGS2"],
    "REFINANCING": ["fred:DGS10", "fred:BAMLH0A0HYM2"],
    "CREDIT_CONDITIONS": ["fred:BAMLH0A0HYM2", "fred:BAMLC0A0CM"],
    "CONSUMER_SPENDING": ["fred:UNRATE", "fred:RSAFS"],
    "ENTERPRISE_SPENDING": ["fred:INDPRO"],
    "INFLATION_INPUT_COSTS": ["fred:CPIAUCSL"],
    "EMPLOYMENT": ["fred:UNRATE", "fred:PAYEMS"],
    "COMMODITY_OIL": ["fred:DCOILWTICO"],
    "COMMODITY_COPPER": ["px:HG=F"],
    "FX_USD": ["fred:DTWEXBGS"],
    "GEOGRAPHY": [], "REGULATION": [], "CUSTOMER_CONCENTRATION": [], "SUPPLIER_CONCENTRATION": [],
}

INDUSTRY_ETFS = {  # SIC ranges -> industry benchmark ETF (only where a reasonable match exists)
    (3674, 3674): "SMH", (7370, 7379): "IGV", (2833, 2836): "XBI", (1311, 1389): "XOP", (1531, 1531): "XHB",
    (4512, 4522): "JETS", (5200, 5999): "XRT", (6020, 6036): "KBE", (6311, 6411): "KIE", (3841, 3845): "IHI",
    (4011, 4731): "IYT", (4810, 4899): "IYZ", (2000, 2099): "PBJ", (3710, 3716): "CARZ",
}


class Exposure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    factor: Factor
    direction: Literal["POSITIVE", "NEGATIVE", "MIXED", "UNKNOWN"]
    magnitude: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"]
    mechanism: str = Field(max_length=1000)
    basis: Literal["EVIDENCED", "ANALYST_ASSUMPTION"]
    evidence: list[ClaimIn] = Field(default_factory=list, max_length=10)
    indicators: list[str] | None = None        # override FACTOR_INDICATORS
    detail: str | None = None                  # e.g. region name, customer name
    valuation_assumption: Literal["wacc", "revenue_growth", "ebit_margin", "none"] = "none"

    @model_validator(mode="after")
    def _basis(self):
        if self.basis == "EVIDENCED" and not self.evidence:
            raise ValueError(f"{self.factor}: EVIDENCED exposures need evidence; otherwise label ANALYST_ASSUMPTION")
        if self.factor == "REFINANCING" and self.direction == "POSITIVE":
            raise ValueError("REFINANCING: rising refinancing cost cannot help the company (use NEGATIVE)")
        return self

    def linked_indicators(self) -> list[str]:
        return self.indicators if self.indicators is not None else FACTOR_INDICATORS[self.factor]


class ExposureProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference_benchmarks: list[str] = Field(default_factory=lambda: list(REFERENCE_ETFS))
    sector: str | None
    sector_benchmark: str | None
    industry_benchmark: str | None = None
    exposures: list[Exposure] = Field(default_factory=list, max_length=30)
    notes: str = ""

    @model_validator(mode="after")
    def _unique(self):
        keys = [(e.factor, e.detail or "") for e in self.exposures]
        dup = {k for k in keys if keys.count(k) > 1}
        if dup:
            raise ValueError(f"duplicate exposures {sorted(dup)}: one entry per factor/detail (edit it instead)")
        return self

    def with_exposure(self, e: Exposure) -> "ExposureProfile":
        """Return a copy where ``e`` replaces any exposure with the same factor/detail."""
        rest = [x for x in self.exposures if (x.factor, x.detail or "") != (e.factor, e.detail or "")]
        return self.model_copy(update={"exposures": rest + [e]})


def industry_etf(sic: str | None) -> str | None:
    if not sic:
        return None
    s = int(sic)
    for (lo, hi), etf in INDUSTRY_ETFS.items():
        if lo <= s <= hi:
            return etf
    return None


SECTOR_HEURISTICS: dict[str, list[tuple[str, str, str, str]]] = {   # factor, direction, magnitude, mechanism
    "Technology": [("ENTERPRISE_SPENDING", "POSITIVE", "MEDIUM", "customers' IT/capex budgets drive demand"),
                   ("FX_USD", "NEGATIVE", "LOW", "foreign revenue translates into fewer dollars when USD strengthens")],
    "Consumer Discretionary": [("CONSUMER_SPENDING", "POSITIVE", "MEDIUM", "discretionary demand tracks household spending"),
                               ("EMPLOYMENT", "POSITIVE", "MEDIUM", "employment supports household income")],
    "Consumer Staples": [("INFLATION_INPUT_COSTS", "NEGATIVE", "MEDIUM", "commodity/packaging costs squeeze margins before price increases")],
    "Energy": [("COMMODITY_OIL", "POSITIVE", "HIGH", "revenue and cash flow scale with realized oil prices")],
    "Financials": [("RATES", "MIXED", "MEDIUM", "net interest margin vs funding costs and asset values"),
                   ("CREDIT_CONDITIONS", "NEGATIVE", "MEDIUM", "credit losses rise when conditions tighten")],
    "Real Estate": [("RATES", "NEGATIVE", "HIGH", "higher yields raise cap rates and financing costs")],
    "Utilities": [("RATES", "NEGATIVE", "MEDIUM", "bond-like valuation and heavy debt financing")],
    "Industrials": [("ENTERPRISE_SPENDING", "POSITIVE", "MEDIUM", "orders follow industrial activity")],
    "Materials": [("COMMODITY_COPPER", "POSITIVE", "MEDIUM", "product prices track industrial metals")],
    "Health Care": [("REGULATION", "NEGATIVE", "MEDIUM", "pricing and reimbursement regulation")],
    "Communication Services": [("CONSUMER_SPENDING", "POSITIVE", "LOW", "advertising and subscriptions follow consumer demand")],
}


def draft_default_profile(app: App, security_id: str, as_of: datetime | None = None) -> ExposureProfile:
    """Starting point for owner review: benchmarks + refinancing from filed facts + sector heuristics (assumptions)."""
    as_of = as_of or app.now()
    r = one(app.conn, "SELECT i.id AS iid, i.sic, i.sector FROM security s JOIN issuer i ON i.id=s.issuer_id WHERE s.id=?",
            (security_id,))
    sector = r["sector"] if r else None
    exposures: list[Exposure] = []
    if r:
        fv = FactView(app, r["iid"], as_of)
        oi = fv.annual("operating_income")
        ie = fv.annual("interest_expense")
        debt_agg, cash_agg = debt_total(fv, as_of.date()), cash_total(fv, as_of.date())
        if oi and debt_agg.value is not None:
            debt = debt_agg.value
            cov = (oi[-1].value / ie[-1].value) if ie and ie[-1].value and ie[-1].end == oi[-1].end else None
            ebit = oi[-1].value
            lev = (debt - (cash_agg.value or 0)) / ebit if ebit and ebit > 0 else None
            mag = "HIGH" if (lev is not None and lev > 3) or (cov is not None and cov < 4) or ebit <= 0 else \
                  "MEDIUM" if (lev is not None and lev > 1.5) else "LOW"
            if debt_agg.missing:
                mag = "UNKNOWN" if mag == "LOW" else mag      # incomplete debt can only understate leverage
            labels = {"long_term_debt_noncurrent": "long-term debt (noncurrent)", "long_term_debt_total": "long-term debt",
                      "long_term_debt_current": "current maturities of long-term debt", "debt_current": "current debt",
                      "short_term_borrowings": "short-term borrowings", "commercial_paper": "commercial paper"}
            facts = [(f"operating income", oi[-1].value, oi[-1].fact_id)] + \
                [(labels[c.lstrip('-')], abs(v), fid) for c, v, fid, _acc in debt_agg.components]
            cits = [Citation(fact_id=fid) for _l, _v, fid in facts if fid]
            txt = "Filed figures: " + "; ".join(f"{lbl} {val:,.0f} USD" for lbl, val, _ in facts)
            exposures.append(Exposure(factor="REFINANCING", direction="NEGATIVE", magnitude=mag,
                                      mechanism="maturing debt must be refinanced at prevailing yields and spreads; "
                                                f"net debt/EBIT {lev:.2f}" if lev is not None else "maturing debt refinanced at prevailing rates",
                                      basis="EVIDENCED",
                                      evidence=[ClaimIn(text=txt, claim_type="FACT", citations=cits)],
                                      valuation_assumption="wacc"))
    for factor, direction, mag, mech in SECTOR_HEURISTICS.get(sector or "", []):
        exposures.append(Exposure(factor=factor, direction=direction, magnitude=mag, mechanism=mech + " (sector heuristic)",
                                  basis="ANALYST_ASSUMPTION"))
    return ExposureProfile(sector=sector, sector_benchmark=SECTOR_ETFS.get(sector or ""),
                           industry_benchmark=industry_etf(r["sic"] if r else None), exposures=exposures,
                           notes="DRAFT generated from filings and sector heuristics; review every item before approval.")


def _verify_exposures(app: App, profile: ExposureProfile, issuer_id: str | None, as_of: datetime) -> list[dict]:
    out = []
    for e in profile.exposures:
        checks = [verify_claim(app, c, issuer_id, as_of) for c in e.evidence] if issuer_id else []
        status = "ASSUMPTION" if e.basis == "ANALYST_ASSUMPTION" else \
            "FAILED" if any(c.status == "FAILED" for c in checks) else \
            "VERIFIED" if checks and all(c.status in ("VERIFIED", "NOT_REQUIRED") for c in checks) else \
            "SOURCE_MATCHED" if any(c.status == "SOURCE_MATCHED" for c in checks) else "UNVERIFIED"
        out.append({"factor": e.factor, "status": status, "verifier_version": VERIFIER_VERSION,
                    "details": [d for c in checks for d in c.details]})
    return out


def _evidenced(ver: list[dict]) -> list[dict]:
    return [v for v in ver if v["status"] != "ASSUMPTION"]


def recheck_evidence(app: App, version_id: str, *, acknowledged: bool = False, reviewer: str | None = None,
                     note: str | None = None) -> list[dict]:
    """Re-verify a profile version's evidence under the CURRENT verifier (at the version's evidence cutoff) and record
    the result as a new, append-only check row. The profile version itself is never modified."""
    r = one(app.conn, "SELECT v.*, s.issuer_id FROM exposure_profile_version v JOIN security s ON s.id=v.security_id "
                      "WHERE v.id=?", (version_id,))
    prof = ExposureProfile.model_validate(from_json(r["content_json"]))
    as_of = datetime.fromisoformat(r["evidence_as_of"].replace("Z", "+00:00"))
    ver = _verify_exposures(app, prof, r["issuer_id"], as_of)
    insert(app.conn, "exposure_evidence_check", {
        "id": new_id("exc"), "exposure_version_id": version_id, "checked_at": app.now_iso(),
        "verifier_version": VERIFIER_VERSION, "verification_json": to_json(ver), "acknowledged": int(acknowledged),
        "reviewer": reviewer, "note": note})
    return ver


def evidence_status(app: App, version_id: str, as_of: str | None = None, *, recheck: bool = True) -> tuple[str, list[str]]:
    """Whether an approved profile may support purchase eligibility at ``as_of``:
    OK | FAILED | REVIEW_REQUIRED, with the factors concerned. The stored snapshot counts only if the current verifier
    produced it; otherwise the latest current-verifier check at or before ``as_of`` is used, and (when ``recheck`` and
    the decision is at the present time) a new check is run. Unresolved items need an acknowledged check whose statuses
    equal the current ones."""
    r = one(app.conn, "SELECT verification_json, created_at FROM exposure_profile_version WHERE id=?", (version_id,))
    stored = from_json(r["verification_json"])
    if not _evidenced(stored):
        return "OK", []                                       # assumption-only profile: nothing to verify
    cutoff = as_of or app.now_iso()
    checks = all_rows(app.conn, "SELECT * FROM exposure_evidence_check WHERE exposure_version_id=? AND verifier_version=? "
                                "AND checked_at<=? ORDER BY checked_at, rowid", (version_id, VERIFIER_VERSION, cutoff))
    if all(v.get("verifier_version") == VERIFIER_VERSION for v in _evidenced(stored)) and r["created_at"] <= cutoff:
        current = stored
    elif checks:
        current = from_json(checks[-1]["verification_json"])
    elif recheck and cutoff >= app.now_iso():
        current = recheck_evidence(app, version_id)
    else:
        return "REVIEW_REQUIRED", [f"{v['factor']}: evidence not checked by verifier {VERIFIER_VERSION}"
                                   for v in _evidenced(stored)]
    failed = [f"{v['factor']} [FAILED]" for v in current if v["status"] == "FAILED"]
    if failed:
        return "FAILED", failed
    weak = {v["factor"]: v["status"] for v in current if v["status"] in ("SOURCE_MATCHED", "UNVERIFIED")}
    if not weak:
        return "OK", []
    acked = [{v["factor"]: v["status"] for v in from_json(c["verification_json"]) if v["status"] != "ASSUMPTION"}
             for c in checks if c["acknowledged"]]
    if any(all(a.get(f) == st for f, st in weak.items()) for a in acked):
        return "OK", []
    return "REVIEW_REQUIRED", [f"{f} [{st}]" for f, st in weak.items()]


def create_profile(app: App, security_id: str, profile: ExposureProfile, *, change_reason: str, author: str = "USER",
                   label: str = "ACTUAL", as_of: datetime | None = None) -> str:
    as_of = as_of or app.now()
    issuer_id = one(app.conn, "SELECT issuer_id FROM security WHERE id=?", (security_id,))["issuer_id"]
    prev = one(app.conn, "SELECT id, version_no FROM exposure_profile_version WHERE security_id=? ORDER BY version_no DESC LIMIT 1",
               (security_id,))
    if prev and not change_reason.strip():
        raise ValueError("a change reason is required for every new exposure-profile version")
    verification = _verify_exposures(app, profile, issuer_id, as_of)
    content = profile.model_dump(mode="json")
    vid = new_id("exv")
    insert(app.conn, "exposure_profile_version", {
        "id": vid, "security_id": security_id, "version_no": (prev["version_no"] + 1) if prev else 1,
        "prev_version_id": prev["id"] if prev else None, "content_json": to_json(content), "content_hash": stable_hash(content),
        "verification_json": to_json(verification), "change_reason": change_reason or "initial exposure profile",
        "author": author, "label": label, "evidence_as_of": iso_utc(as_of), "created_at": app.now_iso(),
    })
    app.audit("exposure.version_created", "exposure_profile_version", vid, {"security_id": security_id})
    return vid


def approve_profile(app: App, version_id: str, approver: str = "owner", note: str = "",
                    acknowledge_unverified: bool = False) -> None:
    """Approve an exposure-profile version. Its evidence is first re-checked under the CURRENT verifier (the stored
    snapshot may come from an older one). FAILED evidence blocks approval (create a corrected version); SOURCE_MATCHED /
    UNVERIFIED evidence needs ``acknowledge_unverified=True``, recorded as an acknowledged check of those exact statuses.
    Re-running on an already approved version records a fresh check/review; it never succeeds silently over FAILED or
    unacknowledged evidence. ANALYST_ASSUMPTION exposures are labelled as such and need no evidence."""
    stored = from_json(one(app.conn, "SELECT verification_json FROM exposure_profile_version WHERE id=?",
                           (version_id,))["verification_json"])
    ver = recheck_evidence(app, version_id) if _evidenced(stored) else stored
    failed = [v["factor"] for v in ver if v["status"] == "FAILED"]
    if failed:
        raise ValueError("exposure evidence failed verification for: " + ", ".join(failed) + "; create a corrected version")
    weak = [f"{v['factor']} [{v['status']}]" for v in ver if v["status"] in ("SOURCE_MATCHED", "UNVERIFIED")]
    if weak and not acknowledge_unverified:
        raise ValueError("exposure evidence is not substantively verified for: " + ", ".join(weak)
                         + "; review it, then pass acknowledge_unverified=True (CLI: --acknowledge-unverified)")
    if weak:
        recheck_evidence(app, version_id, acknowledged=True, reviewer=approver, note=note or "acknowledged at approval")
        note = (note + " | " if note else "") + "acknowledged unverified evidence: " + ", ".join(weak)
    insert(app.conn, "exposure_approval", {"id": new_id("exa"), "exposure_version_id": version_id, "approved_at": app.now_iso(),
                                           "approver": approver, "note": note}, or_ignore=True)
    app.audit("exposure.approved", "exposure_profile_version", version_id, {"note": note})


def current_profile(app: App, security_id: str, as_of: str | None = None) -> tuple[str, ExposureProfile, list[dict]] | None:
    sql = ("SELECT v.* FROM exposure_profile_version v JOIN exposure_approval a ON a.exposure_version_id=v.id "
           "WHERE v.security_id=?")
    params: list = [security_id]
    if as_of:
        sql += " AND a.approved_at<=?"
        params.append(as_of)
    r = one(app.conn, sql + " ORDER BY v.version_no DESC LIMIT 1", params)
    if r is None:
        return None
    return r["id"], ExposureProfile.model_validate(from_json(r["content_json"])), from_json(r["verification_json"])


def profile_history(app: App, security_id: str) -> list[dict]:
    return [dict(r) | {"approved_at": (one(app.conn, "SELECT approved_at FROM exposure_approval WHERE exposure_version_id=?",
                                           (r["id"],)) or {"approved_at": None})["approved_at"]}
            for r in all_rows(app.conn, "SELECT * FROM exposure_profile_version WHERE security_id=? ORDER BY version_no",
                              (security_id,))]
