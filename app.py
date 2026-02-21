from __future__ import annotations

import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import regdiff

app = FastAPI(title="OpenEvidence Diff Monitor", version="0.2")


class CheckRequest(BaseModel):
    company_ids: Optional[List[int]] = Field(
        default=None,
        description="Optional list of company IDs to check. Defaults to watchlist.",
    )
    limit: int = 5
    ordering: str = "-release_datetime"
    diff: bool = True
    sensitivity: str = "aggressive"
    max_lines: Optional[int] = None
    persist: bool = True
    include_markdown: bool = True


class WatchlistEntry(BaseModel):
    company_id: int
    label: Optional[str] = ""


def _client_from_env():
    api_key = os.getenv(regdiff.API_KEY_ENV)
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail=f"Missing API key. Set {regdiff.API_KEY_ENV} in environment.",
        )
    base_url = os.getenv("FINANCIALREPORTS_BASE_URL", regdiff.DEFAULT_BASE_URL)
    user_agent = os.getenv("FINANCIALREPORTS_USER_AGENT", regdiff.DEFAULT_USER_AGENT)
    return regdiff.FinancialReportsClient(api_key=api_key, base_url=base_url, user_agent=user_agent)


def _state_path():
    return os.getenv("REGDIFF_STATE", regdiff.DEFAULT_STATE_PATH)


@app.get("/health")
def health():
    return {"status": "ok", "service": "openevidence-diff", "time": regdiff._now_iso()}


@app.get("/")
def root():
    return {
        "service": "openevidence-diff",
        "status": "ok",
        "docs": "Use /docs for interactive API docs.",
        "endpoints": ["/health", "/watchlist", "/watchlist/add", "/watchlist/remove", "/check"],
    }


@app.get("/favicon.ico")
def favicon():
    return {}


@app.get("/watchlist")
def get_watchlist():
    state = regdiff._load_state(_state_path())
    watchlist = state.get("watchlist", {})
    items = []
    for company_id, meta in sorted(watchlist.items(), key=lambda x: int(x[0])):
        items.append(
            {
                "company_id": int(company_id),
                "label": meta.get("label") or "",
                "added_at": meta.get("added_at"),
            }
        )
    return {"watchlist": items}


@app.post("/watchlist/add")
def add_watchlist(entry: WatchlistEntry):
    state_path = _state_path()
    state = regdiff._load_state(state_path)
    watchlist = state.get("watchlist", {})
    watchlist[str(entry.company_id)] = {
        "label": entry.label or "",
        "added_at": regdiff._now_iso(),
    }
    state["watchlist"] = watchlist
    regdiff._save_state(state_path, state)
    return {"status": "added", "company_id": entry.company_id}


@app.post("/watchlist/remove")
def remove_watchlist(entry: WatchlistEntry):
    state_path = _state_path()
    state = regdiff._load_state(state_path)
    watchlist = state.get("watchlist", {})
    removed = False
    if str(entry.company_id) in watchlist:
        watchlist.pop(str(entry.company_id))
        removed = True
    state["watchlist"] = watchlist
    regdiff._save_state(state_path, state)
    return {"status": "removed" if removed else "not_found", "company_id": entry.company_id}


@app.post("/check")
def run_check(payload: CheckRequest):
    if payload.sensitivity not in regdiff.SENSITIVITY_PRESETS:
        raise HTTPException(status_code=400, detail="Invalid sensitivity value")

    state_path = _state_path()
    state = regdiff._load_state(state_path)

    if payload.company_ids is None:
        watchlist = state.get("watchlist", {})
        company_ids = [int(cid) for cid in watchlist.keys()]
    else:
        company_ids = [int(cid) for cid in payload.company_ids]

    if not company_ids:
        return {"generated_at": regdiff._now_iso(), "results": []}

    client = _client_from_env()
    sensitivity = dict(regdiff.SENSITIVITY_PRESETS[payload.sensitivity])
    if payload.max_lines is not None:
        sensitivity["max_lines"] = payload.max_lines

    markdown_cache = {} if payload.diff else None
    results = []

    for company_id in company_ids:
        label = state.get("watchlist", {}).get(str(company_id), {}).get("label", "")
        data = client.list_filings(
            company_id=company_id,
            limit=payload.limit,
            ordering=payload.ordering,
        )
        items, _ = regdiff._extract_results(data)

        seen = state.get("seen_filings", {}).get(str(company_id), {})
        new_filings = []
        formatted_items = []

        for item in items:
            formatted = regdiff._format_filing(item)
            fid = formatted.get("id")
            if fid is None:
                continue
            fid_key = str(fid)
            if fid_key not in seen:
                new_filings.append(formatted)
            seen[fid_key] = formatted
            formatted_items.append(formatted)

        state.setdefault("seen_filings", {})[str(company_id)] = seen

        diffs = []
        if payload.diff and new_filings:
            baseline_id = state.get("last_filing_id", {}).get(str(company_id))
            if baseline_id is None and len(formatted_items) > 1:
                baseline_id = formatted_items[1].get("id")

            sorted_new = regdiff._sort_filings_by_release(new_filings)
            current_baseline = baseline_id
            for filing in sorted_new:
                filing_id = filing.get("id")
                diff_entry = {
                    "filing_id": filing_id,
                    "baseline_id": current_baseline,
                    "impact_score": 0,
                    "impact_level": "NONE",
                    "section_diffs": [],
                }

                if current_baseline is None:
                    diff_entry["note"] = "No baseline filing available for diff."
                    diffs.append(diff_entry)
                    current_baseline = filing_id
                    continue

                try:
                    old_md = regdiff._get_markdown_cached(markdown_cache, client, current_baseline)
                    new_md = regdiff._get_markdown_cached(markdown_cache, client, filing_id)
                    old_sections = regdiff._parse_markdown_sections(old_md)
                    new_sections = regdiff._parse_markdown_sections(new_md)
                    section_diffs = regdiff._compute_section_diffs(old_sections, new_sections, sensitivity)
                    impact_score = sum(section.get("score", 0) for section in section_diffs)
                    diff_entry["section_diffs"] = section_diffs
                    diff_entry["impact_score"] = impact_score
                    diff_entry["impact_level"] = regdiff._impact_label(impact_score, sensitivity)
                except regdiff.ApiError as exc:
                    diff_entry["note"] = f"Diff unavailable: {exc}"

                diffs.append(diff_entry)
                current_baseline = filing_id

        latest_id = formatted_items[0].get("id") if formatted_items else None
        if latest_id is not None:
            state.setdefault("last_filing_id", {})[str(company_id)] = latest_id

        results.append(
            {
                "company_id": company_id,
                "label": label,
                "new_filings": new_filings,
                "total_checked": len(items),
                "diffs": diffs,
            }
        )

    generated_at = regdiff._now_iso()
    if payload.persist:
        state["last_run"] = generated_at
        regdiff._save_state(state_path, state)

    response = {"generated_at": generated_at, "results": results}
    if payload.include_markdown:
        response["markdown"] = regdiff._render_markdown_report(results, generated_at)
    return response
