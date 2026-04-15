#!/usr/bin/env python3
"""
CSAT Evaluation Workflow
========================
Reads monthly CSAT data from a Google Sheet, evaluates each new period
using Claude (pulling the classification prompt from a Config tab),
writes scores and labels back, computes Trailing 12 flags, and
generates a Summary tab.

SETUP
-----
1. Enable the Google Sheets API in your Google Cloud project.
   https://console.cloud.google.com → APIs & Services → Enable APIs
2. Create a Service Account, download its JSON key, save it as
   credentials.json next to this script.
3. Share your Google Sheet with the service account email address
   (give it Editor access).
4. Paste your sheet's ID into SHEET_ID below.
   (It's the long string in the URL between /d/ and /edit.)
5. Install dependencies:
       pip install gspread anthropic google-auth
6. Set your Anthropic API key:
       export ANTHROPIC_API_KEY="sk-ant-...
7. Run:
       python csat_workflow.py

GOOGLE SHEET STRUCTURE
-----------------------
  Tab "Config":
    A2 = label:  CSAT_CLASSIFICATION_PROMPT
    B2 = value:  (paste your classification prompt here — edit freely)

  Tab "CSAT Data":
    Row 1 = headers (matching the column layout below)
    Row 2+ = one row per calendar month

  Tab "Summary":
    Auto-generated / overwritten each run.
"""

import os
import json
import sys
import gspread
import anthropic
from datetime import datetime, date
from google.oauth2.service_account import Credentials

# ── Configuration ─────────────────────────────────────────────────────────────

SHEET_ID         = "1yYFc7RZLCfKRYp_shbteORtiEUDmhmkv7zU6ilVa4RU"
CREDENTIALS_FILE = "credentials.json"             # service account key file
DATA_TAB         = "CSAT Data"
CONFIG_TAB       = "Config"
SUMMARY_TAB      = "Summary"
PROMPT_LABEL     = "CSAT_CLASSIFICATION_PROMPT"   # label expected in Config!A*
CLAUDE_MODEL     = "claude-opus-4-6"

# ── Column layout (1-indexed, matches your header row in CSAT Data) ───────────

COL_PERIOD           = 1
COL_TOTAL_TICKETS    = 2
COL_CSAT_RESPONSES   = 3
COL_RESPONSE_RATE    = 4
COL_AVG_CSAT         = 5
COL_SCORE1_COUNT     = 6
COL_SCORE1_PCT       = 7
COL_SCORE2_COUNT     = 8
COL_SCORE2_PCT       = 9
COL_SCORE3_COUNT     = 10
COL_SCORE3_PCT       = 11
COL_SCORE4_COUNT     = 12
COL_SCORE4_PCT       = 13
COL_SCORE5_COUNT     = 14
COL_SCORE5_PCT       = 15
COL_SENSITIVITY      = 16
COL_SENSITIVITY_NOTE = 17
COL_LOW_CONC         = 18
COL_CLASSIFICATION   = 19
COL_CONFIDENCE       = 20
COL_KEY_DRIVERS      = 21
COL_EXPLANATION      = 22
COL_TRAILING12_FLAG  = 23
COL_TRAILING12_NOTE  = 24

HEADER_ROW     = 1   # 1-based row index of the header in CSAT Data
FIRST_DATA_ROW = 2   # 1-based row index where data begins

# ── Google Sheets connection ──────────────────────────────────────────────────

def connect(sheet_id: str, credentials_file: str):
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(credentials_file, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id)


# ── Config tab: read the classification prompt ────────────────────────────────

def read_classification_prompt(sheet) -> str:
    """
    Scans column A of the Config tab for a cell whose value is
    CSAT_CLASSIFICATION_PROMPT, then returns the value in column B of the
    same row. This lets you label it clearly without caring about exact cell refs.
    """
    config_ws = sheet.worksheet(CONFIG_TAB)
    labels = config_ws.col_values(1)  # all values in column A
    for i, label in enumerate(labels):
        if label.strip() == PROMPT_LABEL:
            row_num = i + 1  # gspread is 1-indexed
            prompt = config_ws.cell(row_num, 2).value
            if not prompt or not prompt.strip():
                raise ValueError(
                    f"Found '{PROMPT_LABEL}' in Config!A{row_num} "
                    f"but Config!B{row_num} is empty. Please add your prompt."
                )
            return prompt.strip()
    raise ValueError(
        f"Could not find a cell labelled '{PROMPT_LABEL}' in column A "
        f"of the '{CONFIG_TAB}' tab."
    )


# ── Row helpers ───────────────────────────────────────────────────────────────

def get_col(row: list, col: int) -> str:
    idx = col - 1
    if idx < len(row):
        return str(row[idx]).strip()
    return ""


def row_needs_evaluation(row: list) -> bool:
    """A row needs evaluation when its Classification column is empty."""
    return not get_col(row, COL_CLASSIFICATION)


def build_row_context(row: list) -> dict:
    return {
        "period":         get_col(row, COL_PERIOD),
        "total_tickets":  get_col(row, COL_TOTAL_TICKETS),
        "csat_responses": get_col(row, COL_CSAT_RESPONSES),
        "response_rate":  get_col(row, COL_RESPONSE_RATE),
        "avg_csat":       get_col(row, COL_AVG_CSAT),
        "score_1_count":  get_col(row, COL_SCORE1_COUNT),
        "score_1_pct":    get_col(row, COL_SCORE1_PCT),
        "score_2_count":  get_col(row, COL_SCORE2_COUNT),
        "score_2_pct":    get_col(row, COL_SCORE2_PCT),
        "score_3_count":  get_col(row, COL_SCORE3_COUNT),
        "score_3_pct":    get_col(row, COL_SCORE3_PCT),
        "score_4_count":  get_col(row, COL_SCORE4_COUNT),
        "score_4_pct":    get_col(row, COL_SCORE4_PCT),
        "score_5_count":  get_col(row, COL_SCORE5_COUNT),
        "score_5_pct":    get_col(row, COL_SCORE5_PCT),
    }


# ── Claude evaluation ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a CSAT data analyst. You will receive:
1. A classification prompt — the rules you must follow exactly.
2. One month of CSAT data as JSON.

Your job is to evaluate the month and return ONLY a single valid JSON object.
No markdown fences, no commentary, no explanation outside the JSON.

Required keys in your JSON response:
  sensitivity           (number)  — e.g. 0.048
  sensitivity_note      (string)  — plain English, e.g. "Removing 1 response moves avg from 4.2 → 4.1"
  low_score_concentration (string) — e.g. "Diffuse" or "Concentrated — Agent X"
  classification        (string)  — exactly one of: A, B, C, Normal
  confidence            (string)  — exactly one of: High, Medium, Low
  key_drivers           (string)  — pipe-separated short phrases, e.g. "Score 1 spike | Low response rate"
  explanation           (string)  — 2–3 complete sentences
"""


def evaluate_with_claude(client, classification_prompt: str, row_ctx: dict) -> dict:
    user_message = (
        "CLASSIFICATION RULES:\n"
        f"{classification_prompt}\n\n"
        "MONTH DATA:\n"
        f"{json.dumps(row_ctx, indent=2)}\n\n"
        "Evaluate this month according to the rules above. Return JSON only."
    )
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0].strip()
    return json.loads(raw)


# ── Write evaluated fields back to sheet ─────────────────────────────────────

def write_evaluation(ws, sheet_row: int, result: dict):
    """Write all evaluated output columns for one sheet row."""
    updates = {
        COL_SENSITIVITY:      str(result.get("sensitivity", "")),
        COL_SENSITIVITY_NOTE: result.get("sensitivity_note", ""),
        COL_LOW_CONC:         result.get("low_score_concentration", ""),
        COL_CLASSIFICATION:   result.get("classification", ""),
        COL_CONFIDENCE:       result.get("confidence", ""),
        COL_KEY_DRIVERS:      result.get("key_drivers", ""),
        COL_EXPLANATION:      result.get("explanation", ""),
    }
    for col, value in updates.items():
        ws.update_cell(sheet_row, col, value)


# ── Trailing 12 computation ───────────────────────────────────────────────────

CLASSIFICATION_RANK = {"A": 0, "B": 1, "C": 2, "Normal": 3}


def period_to_date(period: str):
    """Convert 'YYYY-MM' to a sortable date. Returns None on failure."""
    try:
        return datetime.strptime(period, "%Y-%m").date()
    except ValueError:
        return None


def compute_trailing12(all_evaluated: list) -> dict:
    """
    Given a list of dicts with keys: period, classification, avg_csat,
    returns a dict keyed by period → {flag: "TRUE"/"FALSE", note: str}.
    """
    dated = []
    for item in all_evaluated:
        d = period_to_date(item["period"])
        if d:
            dated.append((d, item))
    dated.sort(key=lambda x: x[0], reverse=True)

    trailing12 = dated[:12]
    trailing12_periods = {item["period"] for _, item in trailing12}

    if not trailing12:
        return {item["period"]: {"flag": "FALSE", "note": ""} for item in all_evaluated}

    def criticality(t):
        _, item = t
        rank = CLASSIFICATION_RANK.get(item.get("classification", "Normal"), 3)
        try:
            avg = float(item.get("avg_csat", 5))
        except (ValueError, TypeError):
            avg = 5.0
        return (rank, -avg)

    most_critical_date, most_critical_item = min(trailing12, key=criticality)
    flagged_period = most_critical_item["period"]

    today = date.today()
    results = {}

    for item in all_evaluated:
        period = item["period"]
        if period not in trailing12_periods:
            results[period] = {"flag": "FALSE", "note": "Outside trailing 12"}
            continue

        if period == flagged_period:
            d = period_to_date(period)
            months_ago = (
                (today.year - d.year) * 12 + (today.month - d.month)
                if d else 0
            )
            if months_ago == 0:
                when = "current month"
            elif months_ago == 1:
                when = "1 month ago"
            else:
                when = f"{months_ago} months ago"
            results[period] = {
                "flag": "TRUE",
                "note": f"Most critical in trailing 12 — {when}",
            }
        else:
            d_flagged = period_to_date(flagged_period)
            d_this = period_to_date(period)
            if d_flagged and d_this:
                months_diff = abs(
                    (d_this.year - d_flagged.year) * 12
                    + (d_this.month - d_flagged.month)
                )
                ago_str = "1 month ago" if months_diff == 1 else f"{months_diff} months ago"
                results[period] = {
                    "flag": "FALSE",
                    "note": f"Last flagged period — {ago_str}",
                }
            else:
                results[period] = {"flag": "FALSE", "note": ""}

    return results


# ── Summary tab ───────────────────────────────────────────────────────────────

def generate_summary(sheet, all_evaluated: list):
    """Create or overwrite the Summary tab with a snapshot of all evaluated rows."""
    try:
        summary_ws = sheet.worksheet(SUMMARY_TAB)
        summary_ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        summary_ws = sheet.add_worksheet(title=SUMMARY_TAB, rows=200, cols=12)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = [
        "Period", "Avg CSAT", "Response Rate", "CSAT Responses",
        "Total Tickets", "Classification", "Confidence",
        "Key Drivers", "Trailing 12 Flag", "Trailing 12 Note",
    ]
    rows = [[f"CSAT Evaluation Summary — {now}"], [], header]

    for item in sorted(all_evaluated, key=lambda x: x.get("period", ""), reverse=True):
        rows.append([
            item.get("period", ""),
            item.get("avg_csat", ""),
            item.get("response_rate", ""),
            item.get("csat_responses", ""),
            item.get("total_tickets", ""),
            item.get("classification", ""),
            item.get("confidence", ""),
            item.get("key_drivers", ""),
            item.get("trailing12_flag", ""),
            item.get("trailing12_note", ""),
        ])

    summary_ws.update(rows, "A1")
    print(f"  Summary tab written ({len(all_evaluated)} periods).")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("  CSAT Evaluation Workflow")
    print("=" * 50)

    print("\n[1/5] Connecting to Google Sheets...")
    if SHEET_ID == "YOUR_GOOGLE_SHEET_ID_HERE":
        print("ERROR: Please set SHEET_ID in the script before running.")
        sys.exit(1)
    sheet = connect(SHEET_ID, CREDENTIALS_FILE)
    ws = sheet.worksheet(DATA_TAB)
    print(f"  Connected to: {sheet.title}")

    print(f"\n[2/5] Reading '{PROMPT_LABEL}' from '{CONFIG_TAB}' tab...")
    classification_prompt = read_classification_prompt(sheet)
    print(f"  Prompt loaded ({len(classification_prompt)} chars).")

    print(f"\n[3/5] Reading rows from '{DATA_TAB}' tab...")
    all_rows = ws.get_all_values()
    if len(all_rows) < 2:
        print("  No data rows found. Add monthly CSAT data and re-run.")
        sys.exit(0)
    data_rows = all_rows[HEADER_ROW:]

    rows_to_evaluate = [
        (local_idx, row)
        for local_idx, row in enumerate(data_rows)
        if row and get_col(row, COL_PERIOD) and row_needs_evaluation(row)
    ]
    print(f"  Total rows: {len(data_rows)} | Needing evaluation: {len(rows_to_evaluate)}")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: Set the ANTHROPIC_API_KEY environment variable.")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    print(f"\n[4/5] Evaluating {len(rows_to_evaluate)} row(s) with Claude...")
    errors = []
    for local_idx, row in rows_to_evaluate:
        sheet_row = local_idx + FIRST_DATA_ROW
        ctx = build_row_context(row)
        period = ctx["period"] or f"row {sheet_row}"
        print(f"  {period} ...", end=" ", flush=True)
        try:
            result = evaluate_with_claude(client, classification_prompt, ctx)
            write_evaluation(ws, sheet_row, result)
            print(f"✓  {result.get('classification','?')} / {result.get('confidence','?')}")
        except json.JSONDecodeError as e:
            print(f"✗  JSON parse error: {e}")
            errors.append(period)
        except Exception as e:
            print(f"✗  {e}")
            errors.append(period)

    if errors:
        print(f"\n  Warning: {len(errors)} row(s) failed: {', '.join(errors)}")

    print(f"\n[5/5] Computing Trailing 12 flags and generating Summary...")
    fresh_rows = ws.get_all_values()[HEADER_ROW:]

    all_evaluated = []
    for local_idx, row in enumerate(fresh_rows):
        if not row or not get_col(row, COL_PERIOD):
            continue
        if not get_col(row, COL_CLASSIFICATION):
            continue
        ctx = build_row_context(row)
        all_evaluated.append({
            "period":         ctx["period"],
            "avg_csat":       ctx["avg_csat"],
            "response_rate":  ctx["response_rate"],
            "csat_responses": ctx["csat_responses"],
            "total_tickets":  ctx["total_tickets"],
            "classification": get_col(row, COL_CLASSIFICATION),
            "confidence":     get_col(row, COL_CONFIDENCE),
            "key_drivers":    get_col(row, COL_KEY_DRIVERS),
            "explanation":    get_col(row, COL_EXPLANATION),
            "_sheet_row":     local_idx + FIRST_DATA_ROW,
        })

    trailing_results = compute_trailing12(all_evaluated)

    for item in all_evaluated:
        period = item["period"]
        t12 = trailing_results.get(period, {"flag": "", "note": ""})
        sr = item["_sheet_row"]
        ws.update_cell(sr, COL_TRAILING12_FLAG, t12["flag"])
        ws.update_cell(sr, COL_TRAILING12_NOTE, t12["note"])
        item["trailing12_flag"] = t12["flag"]
        item["trailing12_note"] = t12["note"]

    generate_summary(sheet, all_evaluated)

    flagged = [i for i in all_evaluated if i.get("trailing12_flag") == "TRUE"]
    print("\n" + "=" * 50)
    print(f"  Done. {len(all_evaluated)} periods evaluated.")
    if flagged:
        f = flagged[0]
        print(f"  Most critical (trailing 12): {f['period']} "
              f"— {f['classification']} / {f['confidence']}")
    if errors:
        print(f"  Rows with errors: {', '.join(errors)}")
    print("=" * 50)


if __name__ == "__main__":
    main()
