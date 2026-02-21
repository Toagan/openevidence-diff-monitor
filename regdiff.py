#!/usr/bin/env python3
"""Regulatory Diff Monitor for FinancialReports API.

MVP features:
- Manage a watchlist of company IDs
- Check for new filings since the last run
- Emit a Markdown or JSON digest
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE_URL = "https://api.financialreports.eu"
API_KEY_ENV = "FINANCIALREPORTS_API_KEY"
DEFAULT_USER_AGENT = "RegDiffCLI/0.1"
DEFAULT_STATE_PATH = os.path.expanduser("~/.openevidence-diff/state.json")

PRIORITY_SECTION_RULES = [
    ("Risk Factors", ["risk factor", "risk factors", "risks"]),
    ("Management Discussion", ["management discussion", "management's discussion", "md&a", "management commentary", "operating review"]),
    ("Liquidity", ["liquidity", "capital resources", "cash flow", "cash flows"]),
    ("Going Concern", ["going concern"]),
    ("Outlook", ["outlook", "guidance", "forward-looking", "forward looking"]),
]

KEYWORD_WEIGHTS = {
    "going concern": 6,
    "material weakness": 5,
    "restatement": 5,
    "bankrupt": 6,
    "insolv": 5,
    "covenant": 4,
    "default": 4,
    "impairment": 4,
    "investigation": 4,
    "litigation": 4,
    "sanction": 4,
    "fraud": 5,
    "whistleblower": 4,
    "regulatory": 3,
    "liquidity": 3,
    "refinanc": 3,
    "debt": 2,
    "restructur": 3,
    "write-down": 4,
    "write off": 4,
    "guidance": 2,
    "outlook": 2,
    "headwind": 2,
    "downturn": 2,
}

SENSITIVITY_PRESETS = {
    "aggressive": {"min_len": 10, "high": 8, "medium": 4, "max_lines": 8},
    "balanced": {"min_len": 20, "high": 12, "medium": 6, "max_lines": 6},
    "conservative": {"min_len": 30, "high": 16, "medium": 8, "max_lines": 4},
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dumps(value):
    return json.dumps(value, indent=2, ensure_ascii=True)


def _truncate(value, max_len):
    if value is None:
        return ""
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _extract_results(payload):
    if isinstance(payload, dict) and "results" in payload:
        return payload.get("results", []), payload
    if isinstance(payload, list):
        return payload, None
    return [], payload


def _get_first(payload, keys, default=""):
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return default


def _load_state(path):
    if not os.path.exists(path):
        return {
            "watchlist": {},
            "seen_filings": {},
            "last_filing_id": {},
            "last_run": None,
        }
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("watchlist", {})
    data.setdefault("seen_filings", {})
    data.setdefault("last_filing_id", {})
    data.setdefault("last_run", None)
    return data


def _save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, ensure_ascii=True)


class FinancialReportsClient:
    def __init__(self, api_key, base_url=DEFAULT_BASE_URL, user_agent=DEFAULT_USER_AGENT):
        if not api_key:
            raise ValueError("Missing API key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent or DEFAULT_USER_AGENT

    def _request(self, method, path, query=None, body=None):
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"

        headers = {
            "x-api-key": self.api_key,
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", "replace")
            message = error_body
            try:
                parsed = json.loads(error_body)
                if isinstance(parsed, dict):
                    message = parsed.get("detail") or parsed.get("message") or parsed
                else:
                    message = parsed
            except json.JSONDecodeError:
                pass
            raise ApiError(exc.code, message)
        except urllib.error.URLError as exc:
            raise ApiError("network", str(exc.reason))

        text = raw.decode("utf-8", "replace")
        if "application/json" in content_type:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
        return text

    def list_companies(self, search=None, limit=5):
        query = {}
        if search:
            query["search"] = search
        if limit is not None:
            query["limit"] = int(limit)
        return self._request("GET", "/companies/", query=query)

    def list_filings(self, company_id=None, limit=5, ordering=None):
        query = {}
        if company_id is not None:
            query["company_id"] = int(company_id)
        if limit is not None:
            query["limit"] = int(limit)
        if ordering:
            query["ordering"] = ordering
        return self._request("GET", "/filings/", query=query)

    def get_filing_markdown(self, filing_id):
        return self._request("GET", f"/filings/{int(filing_id)}/markdown/")


def _require_api_key():
    api_key = os.getenv(API_KEY_ENV)
    if not api_key:
        raise SystemExit(
            f"Missing API key. Set {API_KEY_ENV} in your environment."
        )
    return api_key


def _render_company_list(items):
    if not items:
        return "No companies found."
    lines = []
    header = f"{'ID':>6}  {'Name':<40}  {'ISIN':<12}  {'Country':<7}"
    lines.append(header)
    lines.append("-" * len(header))
    for item in items:
        company_id = _get_first(item, ["id"], "")
        name = _truncate(_get_first(item, ["name", "company_name"], ""), 40)
        isin = _truncate(_get_first(item, ["isin"], ""), 12)
        country = _truncate(_get_first(item, ["country", "country_code"], ""), 7)
        lines.append(f"{str(company_id):>6}  {name:<40}  {isin:<12}  {country:<7}")
    return "\n".join(lines)


def _print_watchlist(watchlist):
    if not watchlist:
        print("Watchlist is empty.")
        return
    header = f"{'ID':>6}  {'Label':<40}  {'Added':<20}"
    print(header)
    print("-" * len(header))
    for company_id, meta in sorted(watchlist.items(), key=lambda x: int(x[0])):
        label = _truncate(meta.get("label") or "", 40)
        added = _truncate(meta.get("added_at") or "", 20)
        print(f"{company_id:>6}  {label:<40}  {added:<20}")


def _format_filing(item):
    return {
        "id": _get_first(item, ["id"], None),
        "title": _get_first(item, ["title", "name"], ""),
        "release_datetime": _get_first(
            item, ["release_datetime", "release_date", "published_at"], ""
        ),
        "filing_type": _get_first(
            item, ["filing_type_name", "filing_type", "category"], ""
        ),
    }


def _parse_datetime(value):
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _sort_filings_by_release(filings):
    def key(item):
        dt = _parse_datetime(item.get("release_datetime"))
        if dt is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return dt

    return sorted(filings, key=key)


def _normalize_heading(text):
    cleaned = text.lower().strip()
    cleaned = re.sub(r"[^a-z0-9\s&/\-]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _map_section(heading):
    normalized = _normalize_heading(heading)
    for canonical, keywords in PRIORITY_SECTION_RULES:
        for keyword in keywords:
            if keyword in normalized:
                return canonical
    return None


def _parse_markdown_sections(markdown_text):
    sections = {}
    current = None
    for line in markdown_text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if match:
            heading = match.group(2).strip()
            mapped = _map_section(heading)
            current = mapped
            if current:
                sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    output = {}
    for key, lines in sections.items():
        content = "\n".join(lines).strip()
        if content:
            output[key] = content
    return output


def _split_statements(text, min_len):
    statements = []
    seen = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*+\d+.()]+\s+", "", line)
        line = re.sub(r"\s+", " ", line)
        parts = re.split(r"(?<=[.!?])\s+", line)
        for part in parts:
            part = part.strip()
            if len(part) < min_len:
                continue
            if part in seen:
                continue
            seen.add(part)
            statements.append(part)
    return statements


def _score_statements(statements):
    score = 0
    keyword_hits = {}
    for stmt in statements:
        lower = stmt.lower()
        for keyword, weight in KEYWORD_WEIGHTS.items():
            if keyword in lower:
                score += weight
                keyword_hits[keyword] = keyword_hits.get(keyword, 0) + 1
    return score, keyword_hits


def _diff_section(old_text, new_text, min_len, max_lines):
    old_statements = _split_statements(old_text, min_len)
    new_statements = _split_statements(new_text, min_len)
    old_set = set(old_statements)
    new_set = set(new_statements)
    added = [stmt for stmt in new_statements if stmt not in old_set]
    removed = [stmt for stmt in old_statements if stmt not in new_set]

    keyword_score, keyword_hits = _score_statements(added + removed)
    score = len(added) + len(removed) + keyword_score

    return {
        "added": added[:max_lines],
        "removed": removed[:max_lines],
        "added_count": len(added),
        "removed_count": len(removed),
        "score": score,
        "keyword_hits": keyword_hits,
    }


def _impact_label(score, thresholds):
    if score >= thresholds["high"]:
        return "HIGH"
    if score >= thresholds["medium"]:
        return "MEDIUM"
    if score > 0:
        return "LOW"
    return "NONE"


def _compute_section_diffs(old_sections, new_sections, sensitivity):
    diffs = []
    min_len = sensitivity["min_len"]
    max_lines = sensitivity["max_lines"]
    for canonical, _keywords in PRIORITY_SECTION_RULES:
        old_text = old_sections.get(canonical, "")
        new_text = new_sections.get(canonical, "")
        if not old_text and not new_text:
            continue
        diff = _diff_section(old_text, new_text, min_len, max_lines)
        if diff["added_count"] == 0 and diff["removed_count"] == 0:
            continue
        diff["section"] = canonical
        diffs.append(diff)
    return diffs


def _get_markdown_cached(cache, client, filing_id):
    if filing_id in cache:
        return cache[filing_id]
    markdown = client.get_filing_markdown(filing_id)
    cache[filing_id] = markdown
    return markdown


def _render_markdown_report(results, generated_at):
    lines = ["# OpenEvidence Diff Report", f"Generated: {generated_at}", ""]
    if not results:
        lines.append("No companies were checked.")
        return "\n".join(lines)

    any_new = False
    for entry in results:
        label = entry.get("label") or ""
        company_id = entry.get("company_id")
        new_filings = entry.get("new_filings", [])
        total_checked = entry.get("total_checked", 0)
        lines.append(f"## {label} (ID {company_id})")
        lines.append(f"Checked: {total_checked} filings")
        if new_filings:
            any_new = True
            lines.append(f"New filings: {len(new_filings)}")
            for filing in new_filings:
                fid = filing.get("id")
                released = filing.get("release_datetime")
                ftype = filing.get("filing_type")
                title = filing.get("title")
                lines.append(f"- [{fid}] {released} | {ftype} | {title}")
        else:
            lines.append("New filings: 0")

        diffs = entry.get("diffs", [])
        if diffs:
            lines.append("Section-aware diff summary:")
            for diff in diffs:
                filing_id = diff.get("filing_id")
                baseline_id = diff.get("baseline_id")
                impact = diff.get("impact_level")
                score = diff.get("impact_score")
                note = diff.get("note")
                if baseline_id:
                    lines.append(
                        f"Filing {filing_id} vs {baseline_id} | Impact: {impact} (score {score})"
                    )
                else:
                    lines.append(
                        f"Filing {filing_id} | Impact: {impact} (score {score})"
                    )
                if note:
                    lines.append(note)
                section_diffs = diff.get("section_diffs", [])
                if not section_diffs:
                    lines.append("No material section changes detected.")
                    continue
                for section in section_diffs:
                    section_name = section.get("section")
                    section_score = section.get("score")
                    added_count = section.get("added_count")
                    removed_count = section.get("removed_count")
                    lines.append(
                        f"Section: {section_name} (score {section_score}, +{added_count} / -{removed_count})"
                    )
                    added = section.get("added", [])
                    removed = section.get("removed", [])
                    if added:
                        lines.append("Added:")
                        for stmt in added:
                            lines.append(f"- {stmt}")
                    if removed:
                        lines.append("Removed:")
                        for stmt in removed:
                            lines.append(f"- {stmt}")
        lines.append("")

    if not any_new:
        lines.append("No new filings detected across the watchlist.")
    return "\n".join(lines)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="regdiff",
        description="Regulatory Diff Monitor for FinancialReports",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("FINANCIALREPORTS_BASE_URL", DEFAULT_BASE_URL),
        help="Override API base URL (or set FINANCIALREPORTS_BASE_URL)",
    )
    parser.add_argument(
        "--user-agent",
        default=os.getenv("FINANCIALREPORTS_USER_AGENT", DEFAULT_USER_AGENT),
        help="Override User-Agent header (or set FINANCIALREPORTS_USER_AGENT)",
    )
    parser.add_argument(
        "--state",
        default=os.getenv("REGDIFF_STATE", DEFAULT_STATE_PATH),
        help="Path to state file (default: ~/.openevidence-diff/state.json)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    company = sub.add_parser("company", help="Search for companies")
    company.add_argument("query", help="Search term")
    company.add_argument("--limit", type=int, default=5, help="Max results")
    company.add_argument("--json", action="store_true", help="Print raw JSON")

    watch = sub.add_parser("watch", help="Manage watchlist")
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)

    watch_add = watch_sub.add_parser("add", help="Add company to watchlist")
    watch_add.add_argument("company_id", type=int, help="Company id")
    watch_add.add_argument("--label", help="Optional label")

    watch_remove = watch_sub.add_parser("remove", help="Remove company from watchlist")
    watch_remove.add_argument("company_id", type=int, help="Company id")

    watch_sub.add_parser("list", help="List watchlist")

    check = sub.add_parser("check", help="Check for new filings")
    check.add_argument("--limit", type=int, default=5, help="Filings per company")
    check.add_argument(
        "--ordering",
        default="-release_datetime",
        help="Ordering field (default: -release_datetime)",
    )
    check.add_argument(
        "--format",
        choices=["markdown", "json", "both"],
        default="markdown",
        help="Output format",
    )
    check.add_argument(
        "--output",
        help="Write report to file instead of stdout",
    )
    check.add_argument(
        "--json-output",
        help="Write JSON report to file when format is both",
    )
    check.add_argument(
        "--no-save",
        action="store_true",
        help="Do not persist state changes",
    )
    check.add_argument(
        "--diff",
        action="store_true",
        help="Fetch filing markdown and compute section-aware diffs",
    )
    check.add_argument(
        "--sensitivity",
        choices=["aggressive", "balanced", "conservative"],
        default="aggressive",
        help="Sensitivity for materiality scoring (default: aggressive)",
    )
    check.add_argument(
        "--max-lines",
        type=int,
        help="Max statements to show per added/removed list",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    api_key = _require_api_key()
    client = FinancialReportsClient(
        api_key=api_key, base_url=args.base_url, user_agent=args.user_agent
    )

    try:
        if args.command == "company":
            data = client.list_companies(search=args.query, limit=args.limit)
            if args.json:
                print(_json_dumps(data))
                return 0
            items, _ = _extract_results(data)
            print(_render_company_list(items))
            return 0

        if args.command == "watch":
            state = _load_state(args.state)
            watchlist = state.get("watchlist", {})

            if args.watch_command == "list":
                _print_watchlist(watchlist)
                return 0

            if args.watch_command == "add":
                company_id = str(args.company_id)
                watchlist[company_id] = {
                    "label": args.label or "",
                    "added_at": _now_iso(),
                }
                state["watchlist"] = watchlist
                _save_state(args.state, state)
                print(f"Added company {company_id} to watchlist.")
                return 0

            if args.watch_command == "remove":
                company_id = str(args.company_id)
                if company_id in watchlist:
                    watchlist.pop(company_id)
                    state["watchlist"] = watchlist
                    _save_state(args.state, state)
                    print(f"Removed company {company_id} from watchlist.")
                else:
                    print(f"Company {company_id} not in watchlist.")
                return 0

        if args.command == "check":
            state = _load_state(args.state)
            watchlist = state.get("watchlist", {})
            if not watchlist:
                print("Watchlist is empty. Add companies with 'watch add'.")
                return 0

            sensitivity = dict(SENSITIVITY_PRESETS[args.sensitivity])
            if args.max_lines is not None:
                sensitivity["max_lines"] = args.max_lines

            markdown_cache = {} if args.diff else None
            results = []
            for company_id, meta in watchlist.items():
                data = client.list_filings(
                    company_id=int(company_id),
                    limit=args.limit,
                    ordering=args.ordering,
                )
                items, _ = _extract_results(data)
                seen = state.get("seen_filings", {}).get(company_id, {})
                new_filings = []
                formatted_items = []
                for item in items:
                    formatted = _format_filing(item)
                    fid = formatted.get("id")
                    if fid is None:
                        continue
                    fid_key = str(fid)
                    if fid_key not in seen:
                        new_filings.append(formatted)
                    seen[fid_key] = formatted
                    formatted_items.append(formatted)
                state.setdefault("seen_filings", {})[company_id] = seen

                diffs = []
                if args.diff and new_filings:
                    baseline_id = state.get("last_filing_id", {}).get(company_id)
                    if baseline_id is None and len(formatted_items) > 1:
                        baseline_id = formatted_items[1].get("id")

                    sorted_new = _sort_filings_by_release(new_filings)
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
                            old_md = _get_markdown_cached(
                                markdown_cache, client, current_baseline
                            )
                            new_md = _get_markdown_cached(
                                markdown_cache, client, filing_id
                            )
                            old_sections = _parse_markdown_sections(old_md)
                            new_sections = _parse_markdown_sections(new_md)
                            section_diffs = _compute_section_diffs(
                                old_sections, new_sections, sensitivity
                            )
                            impact_score = sum(
                                section.get("score", 0) for section in section_diffs
                            )
                            diff_entry["section_diffs"] = section_diffs
                            diff_entry["impact_score"] = impact_score
                            diff_entry["impact_level"] = _impact_label(
                                impact_score, sensitivity
                            )
                        except ApiError as exc:
                            diff_entry["note"] = f"Diff unavailable: {exc}"

                        diffs.append(diff_entry)
                        current_baseline = filing_id

                latest_id = formatted_items[0].get("id") if formatted_items else None
                if latest_id is not None:
                    state.setdefault("last_filing_id", {})[company_id] = latest_id

                results.append(
                    {
                        "company_id": int(company_id),
                        "label": meta.get("label") or "",
                        "new_filings": new_filings,
                        "total_checked": len(items),
                        "diffs": diffs,
                    }
                )

            generated_at = _now_iso()
            if not args.no_save:
                state["last_run"] = generated_at
                _save_state(args.state, state)

            payload = {"generated_at": generated_at, "results": results}
            markdown_output = _render_markdown_report(results, generated_at)
            json_output = _json_dumps(payload)

            if args.format == "json":
                output = json_output
                if args.output:
                    with open(args.output, "w", encoding="utf-8") as handle:
                        handle.write(output)
                else:
                    print(output)
                return 0

            if args.format == "markdown":
                if args.output:
                    with open(args.output, "w", encoding="utf-8") as handle:
                        handle.write(markdown_output)
                else:
                    print(markdown_output)
                return 0

            if args.format == "both":
                if args.output:
                    with open(args.output, "w", encoding="utf-8") as handle:
                        handle.write(markdown_output)
                else:
                    print(markdown_output)

                json_path = args.json_output
                if not json_path and args.output:
                    json_path = f"{args.output}.json"
                if json_path:
                    with open(json_path, "w", encoding="utf-8") as handle:
                        handle.write(json_output)
                elif not args.output:
                    print("\nJSON:")
                    print(json_output)
            return 0

    except ApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
