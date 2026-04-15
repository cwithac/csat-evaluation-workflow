#!/usr/bin/env python3
"""
CSAT Evaluation Workflow
========================
Reads raw support ticket data from a Google Sheet ("CSAT Data" tab),
aggregates by month using the "Closed At" date, evaluates each month
with Claude, and writes results to the "Summary" tab.

The "CSAT Data" tab is never modified — it is read-only input.
All output goes to the "Summary" tab.

SETUP
-----
1. Enable the Google Sheets API in your Google Cloud project.
   https://console.cloud.google.com → APIs & Services → Enable APIs
2. Create a Service Account, download its JSON key, save it as
   credentials.json next to this script.
3. Share your Google Sheet with the service account email (Editor access).
4. Paste your sheet's ID into SHEET_ID below.
5. Install dependencies:
       pip3 install gspread anthropic google-auth
6. Set your Anthropic API key:
       export ANTHROPIC_API_KEY="sk-ant-..."
7. Run:
       python3 csat_workflow.py

GOOGLE SHEET STRUCTURE
-----------------------
  Tab "Config":
    A2 = label:  CSAT_CLASSIFICATION_PROMPT
    B2 = value:  (your classification prompt)

  Tab "CSAT Data":
    Row 1 = headers (exact column names expected):
      Ticket ID, Ticket Status, Customer Org Name, Customer User Name,
      Org Region, Org ARR Bucket, Issue Priority, Issue Severity,
      Assigned Agent, Assigned Agent Region, Product Area,
      Opened At, Closed At, Resolution Time (days), CSAT Score,
      CSAT Comment, CSAT Response Date
    Row 2+ = one row per support ticket

  Tab "Summary":
    Auto-generated / overwritten each run.
"""

import os
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, date
import gspread
import anthropic
from google.oauth2.service_account import Credentials

# ── Configuration ─────────────────────────────────────────────────────────────

SHEET_ID         = "1yYFc7RZLCfKRYp_shbteORtiEUDmhmkv7zU6ilVa4RU"
CREDENTIALS_FILE = "credentials.json"
DATA_TAB         = "CSAT Data"
CONFIG_TAB       = "Config"
SUMMARY_TAB      = "Summary"
PROMPT_LABEL     = "CSAT_CLASSIFICATION_PROMPT"
CLAUDE_MODEL     = "claude-opus-4-6"

# ── Ticket column names (must match header row in CSAT Data) ──────────────────

COL_TICKET_ID          = "Ticket ID"
COL_TICKET_STATUS      = "Ticket Status"
COL_ORG_NAME           = "Customer Org Name"
COL_USER_NAME          = "Customer User Name"
COL_ORG_REGION         = "Org Region"
COL_ARR_BUCKET         = "Org ARR Bucket"
COL_PRIORITY           = "Issue Priority"
COL_SEVERITY           = "Issue Severity"
COL_AGENT              = "Assigned Agent"
COL_AGENT_REGION       = "Assigned Agent Region"
COL_PRODUCT_AREA       = "Product Area"
COL_OPENED_AT          = "Created Date"
COL_CLOSED_AT          = "Solved Date"
COL_RESOLUTION_TIME    = "Resolution Time (days)"
COL_CSAT_SCORE         = "CSAT Score"
COL_CSAT_COMMENT       = "CSAT Comment"
COL_CSAT_RESPONSE_DATE = "CSAT Response Date"

# ── Google Sheets connection ──────────────────────────────────────────────────

def connect(sheet_id: str, credentials_file: str):
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(credentials_file, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id)


# ── Config tab ────────────────────────────────────────────────────────────────

def read_classification_prompt(sheet) -> str:
    config_ws = sheet.worksheet(CONFIG_TAB)
    labels = config_ws.col_values(1)
    for i, label in enumerate(labels):
        if label.strip() == PROMPT_LABEL:
            row_num = i + 1
            prompt = config_ws.cell(row_num, 2).value
            if not prompt or not prompt.strip():
                raise ValueError(
                    f"Found '{PROMPT_LABEL}' in Config!A{row_num} "
                    f"but Config!B{row_num} is empty."
                )
            return prompt.strip()
    raise ValueError(f"Could not find '{PROMPT_LABEL}' in Config tab column A.")


def write_last_run_timestamp(sheet):
    """
    Write the current timestamp to Config!A3/B3 so Zapier can watch
    cell B3 for changes and trigger a Zap on every workflow run.
    Also writes a summary generated note to Config!A4/B4.
    """
    config_ws = sheet.worksheet(CONFIG_TAB)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_short = datetime.now().strftime("%Y-%m-%d %H:%M")
    config_ws.update("A3", [["LAST_RUN"]])
    config_ws.update("B3", [[now]])
    config_ws.update("A4", [["SUMMARY_GENERATED"]])
    config_ws.update("B4", [[f"CSAT Evaluation Summary — generated {now_short}"]])
    print(f"  Zapier trigger updated: LAST_RUN = {now}")
    print(f"  Summary note written to Config tab: generated {now_short}")


# ── Date parsing ──────────────────────────────────────────────────────────────

DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%d/%m/%Y",
]

def parse_date(value):
    """Try to parse a date string, return datetime or None."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None

def date_to_period(dt) -> str:
    """Convert datetime to 'YYYY-MM' period string."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m")

def period_to_label(period: str) -> str:
    """Convert 'YYYY-MM' to 'Mon YYYY' for display."""
    try:
        return datetime.strptime(period, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return period


# ── Ticket data aggregation ───────────────────────────────────────────────────

def parse_score(val) -> float:
    """Parse CSAT score from string or number, return float or None."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).strip())
    except (ValueError, TypeError):
        return None


def format_breakdown(items, total_low: int) -> str:
    if not items:
        return "None"
    parts = []
    for name, count in items:
        pct = round(count / total_low * 100) if total_low else 0
        parts.append(f"{name} ({count}, {pct}%)")
    return " | ".join(parts)


def debug_date_sample(all_rows: list):
    """Print a sample of Closed At values to help diagnose parse failures."""
    print("  Sample 'Closed At' values from your data:")
    seen = set()
    for row in all_rows[:50]:
        val = str(row.get(COL_CLOSED_AT, "")).strip()
        if val and val not in seen:
            seen.add(val)
            print(f"    {repr(val)}")
        if len(seen) >= 5:
            break


def aggregate_tickets(all_rows: list) -> dict:
    """
    Group ticket rows by month (Solved Date) and compute aggregates.
    Excludes the current calendar month — only fully closed months are evaluated.
    Returns dict keyed by period ('YYYY-MM') -> aggregate dict.
    """
    current_period = date.today().strftime("%Y-%m")

    by_period = defaultdict(list)
    skipped = 0
    excluded_current = 0
    for row in all_rows:
        closed_val = row.get(COL_CLOSED_AT, "")
        dt = parse_date(closed_val)
        period = date_to_period(dt)
        if period:
            if period >= current_period:
                excluded_current += 1  # skip current (incomplete) month
            else:
                by_period[period].append(row)
        else:
            skipped += 1

    if excluded_current:
        print(f"  Excluded {excluded_current} ticket(s) from {current_period} (month not yet complete).")
    if skipped:
        print(f"  Warning: {skipped} ticket(s) skipped (unparseable Solved Date).")

    aggregates = {}
    for period, tickets in sorted(by_period.items()):
        total = len(tickets)

        scored = []
        for t in tickets:
            score = parse_score(t.get(COL_CSAT_SCORE))
            if score is not None:
                scored.append((score, t))

        csat_responses = len(scored)
        response_rate = round(csat_responses / total, 4) if total else 0
        avg_csat = round(sum(s for s, _ in scored) / csat_responses, 3) if csat_responses else None

        score_counts = defaultdict(int)
        for score, _ in scored:
            bucket = max(1, min(5, round(score)))
            score_counts[bucket] += 1

        low_score_tickets = [(s, t) for s, t in scored if s <= 2]
        agent_low = defaultdict(int)
        product_low = defaultdict(int)
        for _, t in low_score_tickets:
            agent = str(t.get(COL_AGENT, "") or "Unknown").strip() or "Unknown"
            product = str(t.get(COL_PRODUCT_AREA, "") or "Unknown").strip() or "Unknown"
            agent_low[agent] += 1
            product_low[product] += 1

        top_agents = sorted(agent_low.items(), key=lambda x: -x[1])[:3]
        top_products = sorted(product_low.items(), key=lambda x: -x[1])[:3]
        total_low = len(low_score_tickets)

        aggregates[period] = {
            "period":           period,
            "period_label":     period_to_label(period),
            "total_tickets":    total,
            "csat_responses":   csat_responses,
            "response_rate":    response_rate,
            "avg_csat":         avg_csat,
            "score_1_count":    score_counts[1],
            "score_1_pct":      round(score_counts[1] / csat_responses, 4) if csat_responses else 0,
            "score_2_count":    score_counts[2],
            "score_2_pct":      round(score_counts[2] / csat_responses, 4) if csat_responses else 0,
            "score_3_count":    score_counts[3],
            "score_3_pct":      round(score_counts[3] / csat_responses, 4) if csat_responses else 0,
            "score_4_count":    score_counts[4],
            "score_4_pct":      round(score_counts[4] / csat_responses, 4) if csat_responses else 0,
            "score_5_count":    score_counts[5],
            "score_5_pct":      round(score_counts[5] / csat_responses, 4) if csat_responses else 0,
            "total_low_scores": total_low,
            "top_agents_low_scores":         format_breakdown(top_agents, total_low),
            "top_product_areas_low_scores":  format_breakdown(top_products, total_low),
        }

    return aggregates


# ── Read already-evaluated periods from Summary tab ───────────────────────────

def read_evaluated_periods(sheet) -> dict:
    """
    Read the Summary tab and return a dict of period → row_dict
    for all periods that already have a Classification value.
    Layout: row 1 = headers, row 2+ = data.
    """
    current_period = date.today().strftime("%Y-%m")
    try:
        summary_ws = sheet.worksheet(SUMMARY_TAB)
        all_values = summary_ws.get_all_values()
        if len(all_values) < 2:
            return {}
        headers = all_values[0]
        result = {}
        for row in all_values[1:]:
            if len(row) < len(headers):
                row = row + [""] * (len(headers) - len(row))
            row_dict = dict(zip(headers, row))
            period = row_dict.get("Period", "").strip()
            classification = row_dict.get("Classification", "").strip()
            # Exclude current and future months — only fully closed months belong here
            if period and classification and period < current_period:
                result[period] = row_dict
        return result
    except gspread.exceptions.WorksheetNotFound:
        return {}


# ── Claude evaluation ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a CSAT data analyst. You will receive:
1. A classification prompt — the rules you must follow exactly.
2. One month of aggregated CSAT data as JSON.

The data includes breakdowns of which agents and product areas had the most
low scores (1s and 2s), which you should use to assess low score concentration.

Your job is to evaluate the month and return ONLY a single valid JSON object.
No markdown fences, no commentary, no explanation outside the JSON.

Required keys in your JSON response:
  sensitivity             (number)  — e.g. 0.048
  sensitivity_note        (string)  — plain English, e.g. "Removing 1 response moves avg from 4.2 → 4.1"
  low_score_concentration (string)  — e.g. "Diffuse" or "Concentrated — Agent X (3 of 5 low scores)"
  classification          (string)  — exactly one of: A, B, C, Normal
  confidence              (string)  — exactly one of: High, Medium, Low
  key_drivers             (string)  — pipe-separated short phrases, e.g. "Score 1 spike | Low response rate"
  explanation             (string)  — 2–3 complete sentences
"""


def evaluate_with_claude(client, classification_prompt: str, agg: dict) -> dict:
    ctx = {k: v for k, v in agg.items() if not k.startswith("_")}
    user_message = (
        "CLASSIFICATION RULES:\n"
        f"{classification_prompt}\n\n"
        "MONTH DATA:\n"
        f"{json.dumps(ctx, indent=2)}\n\n"
        "Evaluate this month according to the rules above. Return JSON only."
    )
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0].strip()
    return json.loads(raw)


# ── Trailing 12 computation ───────────────────────────────────────────────────

CLASSIFICATION_RANK = {"A": 0, "B": 1, "C": 2, "Normal": 3}


def period_to_date(period: str):
    try:
        return datetime.strptime(period, "%Y-%m").date()
    except ValueError:
        return None


def compute_trailing12(all_evaluated: list) -> dict:
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
            avg = float(item.get("avg_csat") or 5)
        except (ValueError, TypeError):
            avg = 5.0
        return (rank, -avg)

    _, most_critical_item = min(trailing12, key=criticality)
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
                s = "s" if months_diff != 1 else ""
                results[period] = {
                    "flag": "FALSE",
                    "note": f"Last flagged: {flagged_period} ({months_diff} month{s} away)",
                }
            else:
                results[period] = {"flag": "FALSE", "note": ""}

    return results


# ── Summary tab ───────────────────────────────────────────────────────────────

SUMMARY_HEADERS = [
    "Period", "Period Label", "Total Tickets", "CSAT Responses",
    "Response Rate", "Avg CSAT",
    "Score 1 Count", "Score 1 %", "Score 2 Count", "Score 2 %",
    "Score 3 Count", "Score 3 %", "Score 4 Count", "Score 4 %",
    "Score 5 Count", "Score 5 %",
    "Sensitivity", "Sensitivity Note", "Low Score Concentration",
    "Classification", "Confidence", "Key Drivers", "Explanation",
    "Trailing 12 Flag", "Trailing 12 Note",
    "Top Agents (Low Scores)", "Top Product Areas (Low Scores)",
]


def pct_fmt(val) -> str:
    try:
        return f"{float(val) * 100:.1f}%"
    except (TypeError, ValueError):
        return ""


def generate_summary(sheet, all_evaluated: list):
    try:
        summary_ws = sheet.worksheet(SUMMARY_TAB)
        summary_ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        summary_ws = sheet.add_worksheet(
            title=SUMMARY_TAB, rows=500, cols=len(SUMMARY_HEADERS) + 2
        )

    rows = [
        SUMMARY_HEADERS,
    ]

    for item in sorted(all_evaluated, key=lambda x: x.get("period", ""), reverse=True):
        rows.append([
            item.get("period", ""),
            item.get("period_label", ""),
            item.get("total_tickets", ""),
            item.get("csat_responses", ""),
            pct_fmt(item.get("response_rate")),
            item.get("avg_csat", ""),
            item.get("score_1_count", ""), pct_fmt(item.get("score_1_pct")),
            item.get("score_2_count", ""), pct_fmt(item.get("score_2_pct")),
            item.get("score_3_count", ""), pct_fmt(item.get("score_3_pct")),
            item.get("score_4_count", ""), pct_fmt(item.get("score_4_pct")),
            item.get("score_5_count", ""), pct_fmt(item.get("score_5_pct")),
            item.get("sensitivity", ""),
            item.get("sensitivity_note", ""),
            item.get("low_score_concentration", ""),
            item.get("classification", ""),
            item.get("confidence", ""),
            item.get("key_drivers", ""),
            item.get("explanation", ""),
            item.get("trailing12_flag", ""),
            item.get("trailing12_note", ""),
            item.get("top_agents_low_scores", ""),
            item.get("top_product_areas_low_scores", ""),
        ])

    summary_ws.update(rows, "A1")
    print(f"  Summary tab written ({len(all_evaluated)} periods).")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("  CSAT Evaluation Workflow")
    print("=" * 50)

    print("\n[1/5] Connecting to Google Sheets...")
    sheet = connect(SHEET_ID, CREDENTIALS_FILE)
    ws = sheet.worksheet(DATA_TAB)
    print(f"  Connected to: {sheet.title}")

    print(f"\n[2/5] Reading '{PROMPT_LABEL}' from '{CONFIG_TAB}' tab...")
    classification_prompt = read_classification_prompt(sheet)
    print(f"  Prompt loaded ({len(classification_prompt)} chars).")

    print(f"\n[3/5] Reading and aggregating tickets from '{DATA_TAB}' tab...")
    all_rows = ws.get_all_records()
    if not all_rows:
        print("  No ticket data found. Import your CSV into CSAT Data and re-run.")
        sys.exit(0)
    print(f"  {len(all_rows)} tickets found.")

    aggregates = aggregate_tickets(all_rows)
    if not aggregates:
        print("  No valid monthly periods found. Check the 'Closed At' date format.")
        debug_date_sample(all_rows)
        sys.exit(1)

    period_list = ", ".join(sorted(aggregates.keys()))
    print(f"  Aggregated into {len(aggregates)} monthly periods: {period_list}")

    already_evaluated = read_evaluated_periods(sheet)
    periods_to_evaluate = [p for p in sorted(aggregates.keys()) if p not in already_evaluated]
    print(f"  Already evaluated: {len(already_evaluated)} | New periods: {len(periods_to_evaluate)}")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: Set the ANTHROPIC_API_KEY environment variable.")
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)

    newly_evaluated = {}
    errors = []
    if periods_to_evaluate:
        print(f"\n[4/5] Evaluating {len(periods_to_evaluate)} period(s) with Claude...")
        for period in periods_to_evaluate:
            agg = aggregates[period]
            label = agg["period_label"]
            print(f"  {label} ({period}) ...", end=" ", flush=True)
            try:
                result = evaluate_with_claude(client, classification_prompt, agg)
                agg.update(result)
                newly_evaluated[period] = agg
                print(f"✓  {result.get('classification','?')} / {result.get('confidence','?')}")
                time.sleep(0.5)
            except json.JSONDecodeError as e:
                print(f"✗  JSON parse error: {e}")
                errors.append(period)
            except Exception as e:
                print(f"✗  {e}")
                errors.append(period)

        if errors:
            print(f"\n  Warning: {len(errors)} period(s) failed: {', '.join(errors)}")
    else:
        print(f"\n[4/5] No new periods to evaluate — all months already in Summary.")

    print(f"\n[5/5] Computing Trailing 12 flags and generating Summary...")

    all_evaluated = []

    for period, summary_row in already_evaluated.items():
        agg = dict(aggregates.get(period, {}))
        agg["period"] = period
        agg["period_label"] = period_to_label(period)
        agg["sensitivity"]             = summary_row.get("Sensitivity", "")
        agg["sensitivity_note"]        = summary_row.get("Sensitivity Note", "")
        agg["low_score_concentration"] = summary_row.get("Low Score Concentration", "")
        agg["classification"]          = summary_row.get("Classification", "")
        agg["confidence"]              = summary_row.get("Confidence", "")
        agg["key_drivers"]             = summary_row.get("Key Drivers", "")
        agg["explanation"]             = summary_row.get("Explanation", "")
        all_evaluated.append(agg)

    for period, agg in newly_evaluated.items():
        all_evaluated.append(agg)

    trailing_results = compute_trailing12(all_evaluated)
    for item in all_evaluated:
        t12 = trailing_results.get(item["period"], {"flag": "FALSE", "note": ""})
        item["trailing12_flag"] = t12["flag"]
        item["trailing12_note"] = t12["note"]

    generate_summary(sheet, all_evaluated)
    write_last_run_timestamp(sheet)

    flagged = [i for i in all_evaluated if i.get("trailing12_flag") == "TRUE"]
    print("\n" + "=" * 50)
    print(f"  Done. {len(all_evaluated)} period(s) in Summary.")
    if flagged:
        f = flagged[0]
        print(f"  Most critical (trailing 12): {f['period']} "
              f"— {f.get('classification','?')} / {f.get('confidence','?')}")
    if errors:
        print(f"  Periods with errors: {', '.join(errors)}")
    print("=" * 50)


if __name__ == "__main__":
    main()
