# OpenEvidence Diff Monitor (MVP)

CLI that tracks new filings for a watchlist of companies using the FinancialReports API.

## Features

- Watchlist management
- Check for new filings since last run
- Section-aware diffs with materiality scoring (optional)
- Markdown or JSON digest output (or both)

## Setup

```bash
export FINANCIALREPORTS_API_KEY="YOUR_KEY"
```

Optional overrides:

```bash
export FINANCIALREPORTS_BASE_URL="https://api.financialreports.eu"
export FINANCIALREPORTS_USER_AGENT="RegDiffCLI/0.1"
export REGDIFF_STATE="$HOME/.openevidence-diff/state.json"
```

## Usage

Search for company IDs:

```bash
python3 regdiff.py company "Siemens"
```

Manage the watchlist:

```bash
python3 regdiff.py watch add 12345 --label "Siemens"
python3 regdiff.py watch list
python3 regdiff.py watch remove 12345
```

Check for new filings:

```bash
python3 regdiff.py check --limit 5
python3 regdiff.py check --format json --output report.json
python3 regdiff.py check --diff --format both --output report.md
```

## Output

The Markdown report includes a section per company and lists new filings by ID, release time, type, and title. When `--diff` is enabled, it adds section-aware diffs with an impact score. JSON output includes the same data under `results`.

## Notes

- State is stored in `~/.openevidence-diff/state.json` by default.
- This CLI only detects *new* filings since the last run (based on filing IDs).
 - Use `--sensitivity aggressive|balanced|conservative` to tune scoring.
 - Use `--json-output` with `--format both` to control JSON output path.
