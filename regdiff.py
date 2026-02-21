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
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_BASE_URL = "https://api.financialreports.eu"
API_KEY_ENV = "FINANCIALREPORTS_API_KEY"
DEFAULT_USER_AGENT = "RegDiffCLI/0.1"
DEFAULT_STATE_PATH = os.path.expanduser("~/.openevidence-diff/state.json")


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
        return {"watchlist": {}, "seen_filings": {}, "last_run": None}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("watchlist", {})
    data.setdefault("seen_filings", {})
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
        choices=["markdown", "json"],
        default="markdown",
        help="Output format",
    )
    check.add_argument(
        "--output",
        help="Write report to file instead of stdout",
    )
    check.add_argument(
        "--no-save",
        action="store_true",
        help="Do not persist state changes",
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
                for item in items:
                    formatted = _format_filing(item)
                    fid = formatted.get("id")
                    if fid is None:
                        continue
                    fid_key = str(fid)
                    if fid_key not in seen:
                        new_filings.append(formatted)
                    seen[fid_key] = formatted
                state.setdefault("seen_filings", {})[company_id] = seen

                results.append(
                    {
                        "company_id": int(company_id),
                        "label": meta.get("label") or "",
                        "new_filings": new_filings,
                        "total_checked": len(items),
                    }
                )

            generated_at = _now_iso()
            if not args.no_save:
                state["last_run"] = generated_at
                _save_state(args.state, state)

            if args.format == "json":
                output = _json_dumps({"generated_at": generated_at, "results": results})
            else:
                output = _render_markdown_report(results, generated_at)

            if args.output:
                with open(args.output, "w", encoding="utf-8") as handle:
                    handle.write(output)
            else:
                print(output)
            return 0

    except ApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
