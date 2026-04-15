# csat-evaluation-workflow

An AI-driven workflow that evaluates monthly CSAT data using Claude, writes scores and labels back to Google Sheets, and surfaces the most critical period across a trailing 12-month window.

Built for support and customer experience managers who need more than a raw number — context, confidence, and a clear signal about where to focus.

---

## The problem it solves

Monthly CSAT scores land in a spreadsheet. Someone has to look at them, decide if they're bad enough to act on, figure out why, and remember what happened three months ago. That process is manual, inconsistent, and easy to deprioritize.

This workflow automates the evaluation layer. Each month gets a classification (A / B / C / Normal), a confidence rating, a sensitivity note, key drivers, and a plain-English explanation — all written by Claude, based on rules you define.

---

## How the AI fits in

The classification logic lives in a cell in your Google Sheet — not in the code. You write the rules in plain English in a cell labelled `CSAT_CLASSIFICATION_PROMPT`. Claude reads them at runtime and applies them to each month's data.

If a threshold changes, a new factor matters, or you want to reason differently about low response rates, you edit the cell. No code changes. No redeployment.

---

## What it produces

For each month, the workflow evaluates and writes back:

| Field | Description |
|---|---|
| Classification | A (Critical), B (At Risk), C (Below Target), or Normal |
| Confidence | High / Medium / Low based on sample size and signal clarity |
| Sensitivity | How much one response shifts the average |
| Sensitivity Note | Plain English — "Removing 1 response moves avg from 4.2 → 4.1" |
| Low Score Concentration | Whether low scores appear diffuse or concentrated |
| Key Drivers | Pipe-separated list of contributing factors |
| Explanation | 2–3 sentence summary of why this month was classified as it was |
| Trailing 12 Flag | TRUE for the single most critical month in the last 12 |
| Trailing 12 Note | Context on when the flagged period occurred |

A **Summary tab** is generated automatically on every run.

---

## Key design decisions

- **On-demand, not scheduled** — run it when you have new data
- **Rules in the sheet, not the code** — the person closest to the business owns the logic
- **Skips already-evaluated rows** — safe to re-run; only processes rows with an empty Classification column
- **Trailing 12 is always recalculated** — flags update across all rows on every run

---

## Setup

See the detailed instructions at the top of `csat_workflow.py`. You will need:

- A Google Cloud project with the Sheets API enabled
- A service account key (`credentials.json`) with Editor access to your sheet
- An Anthropic API key
- Python 3.8+

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
python csat_workflow.py
```

---

## Files

| File | Purpose |
|---|---|
| `csat_workflow.py` | Main workflow script |
| `CSAT_CLASSIFICATION_PROMPT_default.txt` | Starter classification rules — paste into your Config tab |
| `requirements.txt` | Python dependencies |
| `.gitignore` | Keeps credentials out of version control |
