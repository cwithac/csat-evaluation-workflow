# Claude AI Prompt — CSAT Evaluation Workflow

This document captures the prompt and context used to build the `csat_workflow.py` script with Claude. You can use this as a starting point to recreate, adapt, or extend the workflow for your own use case.

---

## The Core Prompt

> Build a Python script that reads raw support ticket data from a Google Sheet, aggregates it by month, evaluates each month using the Claude API, and writes the results back to a Summary tab in the same sheet.
>
> **Rules:**
> - The source data tab ("CSAT Data") must never be modified — it is read-only input.
> - All output goes to the "Summary" tab, which is fully overwritten on each run.
> - Only evaluate fully completed months. Skip the current calendar month since it isn't closed yet.
> - Don't re-evaluate months that already have a Classification in the Summary tab — preserve existing evaluations and only call the Claude API for new periods.
> - Read the classification prompt from a Config tab in the sheet (not hardcoded), so it can be updated without touching the script.
> - Write a timestamp to the Config tab on every run so a Zapier automation can watch for changes and trigger a Zap.

---

## Google Sheet Structure

The script expects a Google Sheet with three tabs:

### Config tab
| Column A | Column B |
|---|---|
| CSAT_CLASSIFICATION_PROMPT | *(your classification rules for Claude)* |
| LAST_RUN | *(auto-written by the script on each run)* |
| SUMMARY_GENERATED | *(auto-written: "CSAT Evaluation Summary — generated YYYY-MM-DD HH:MM")* |

### CSAT Data tab
Row 1 = headers. The script expects these exact column names:

`Ticket ID`, `Ticket Status`, `Customer Org Name`, `Customer User Name`, `Org Region`, `Org ARR Bucket`, `Issue Priority`, `Issue Severity`, `Assigned Agent`, `Assigned Agent Region`, `Product Area`, `Created Date`, `Solved Date`, `Resolution Time (days)`, `CSAT Score`, `CSAT Comment`, `CSAT Response Date`

Row 2+ = one row per support ticket.

### Summary tab
Auto-generated and fully overwritten on each run. Headers start at row 1, data at row 2. Columns written:

`Period`, `Period Label`, `Total Tickets`, `CSAT Responses`, `Response Rate`, `Avg CSAT`, `Score 1–5 Count & %` (10 columns), `Sensitivity`, `Sensitivity Note`, `Low Score Concentration`, `Classification`, `Confidence`, `Key Drivers`, `Explanation`, `Trailing 12 Flag`, `Trailing 12 Note`, `Top Agents (Low Scores)`, `Top Product Areas (Low Scores)`

---

## What Claude Evaluates

For each month, the script sends aggregated data to Claude (not raw ticket rows) and asks it to return a JSON object with these fields:

| Field | Type | Description |
|---|---|---|
| `sensitivity` | number | How much one response would shift the avg CSAT |
| `sensitivity_note` | string | Plain English explanation of sensitivity |
| `low_score_concentration` | string | e.g. "Diffuse" or "Concentrated — Agent X (3 of 5)" |
| `classification` | string | Exactly one of: `A`, `B`, `C`, `Normal` |
| `confidence` | string | Exactly one of: `High`, `Medium`, `Low` |
| `key_drivers` | string | Pipe-separated short phrases |
| `explanation` | string | 2–3 complete sentences |

The classification rules (what makes a month A vs B vs Normal, etc.) live in the Config tab as `CSAT_CLASSIFICATION_PROMPT` — not in the script — so they can be tuned without a code change.

---

## Trailing 12 Logic

After all months are evaluated, the script computes a "Trailing 12" flag:

- Looks at the 12 most recent completed months.
- Identifies the single most critical month (lowest classification rank, then lowest avg CSAT as a tiebreaker).
- Sets `Trailing 12 Flag = TRUE` for that period only; all others get `FALSE`.
- Writes a note explaining how far away the flagged period is (e.g. "Most critical in trailing 12 — 3 months ago").

---

## Setup Requirements

1. **Google Cloud**: Enable the Google Sheets API, create a Service Account, download `credentials.json`, and share the sheet with the service account email (Editor access).
2. **Dependencies**: `pip3 install gspread anthropic google-auth`
3. **Environment variable**: `export ANTHROPIC_API_KEY="sk-ant-..."`
4. **Config**: Set `SHEET_ID` in the script to your Google Sheet's ID.

---

## Iterations Made During Development

These are changes made after the initial build that you may want to carry forward or adapt:

- **Header row moved to row 1** — Originally the Summary tab had a generated title in row 1, a blank row 2, and headers in row 3. This was reorganized so headers start at row 1 and data at row 2, removing the whitespace that blocked other work.
- **Generated note moved to Config tab** — The "CSAT Evaluation Summary — generated [timestamp]" note was removed from the Summary tab and written to Config!A4/B4 instead.
- **Zapier trigger** — The script writes `LAST_RUN` + timestamp to Config!A3/B3 on every run so Zapier can watch cell B3 and trigger automations.
- **Warning suppression** — `warnings.filterwarnings()` calls added at the top of the script to silence deprecation and SSL warnings from third-party libraries (Python 3.9 + google-auth + urllib3).

---

## Model Used

`claude-opus-4-6` — set as `CLAUDE_MODEL` constant in the script, easy to swap.
