"""
S.3971 SBIR/STTR Reauthorization Act - Status Monitoring Agent
Checks Congress.gov daily, detects changes, sends Gmail alerts.
"""

import os
import json
import smtplib
import requests
import logging
from datetime import datetime, timezone
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from bs4 import BeautifulSoup

# ── Configuration ─────────────────────────────────────────────────────────────

BILL_URL        = "https://www.congress.gov/bill/118th-congress/senate-bill/3971"
ACTIONS_URL     = "https://www.congress.gov/bill/118th-congress/senate-bill/3971/all-actions"
RECIPIENT_EMAIL = "zekunliu99@gmail.com"
SENDER_EMAIL    = os.environ.get("GMAIL_SENDER", "")
GMAIL_APP_PASS  = os.environ.get("GMAIL_APP_PASSWORD", "")
STATE_FILE      = Path(os.environ.get("STATE_FILE", "bill_state.json"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ── Status Classification ──────────────────────────────────────────────────────

STATUS_RULES = [
    # (keyword_list, canonical_status, is_critical)
    (["signed into law", "became public law", "public law"],           "Signed into Law",        True),
    (["presented to president", "sent to president", "enrolled bill"], "Sent to President",      True),
    (["passed house", "house passed"],                                 "Passed House",            True),
    (["failed in house", "house failed", "defeated in house"],        "Failed in House",         True),
    (["passed senate", "senate passed"],                               "Passed Senate",           False),
    (["failed in senate", "senate failed"],                            "Failed in Senate",        True),
    (["postponed", "no vote", "tabled"],                               "Vote Postponed / No Vote Yet", False),
    (["placed on calendar", "calendar"],                               "Placed on Calendar",      False),
    (["committee", "referred to"],                                     "In Committee",            False),
    (["introduced"],                                                   "Introduced",              False),
]

CRITICAL_STATUSES = {rule[1] for rule in STATUS_RULES if rule[2]}


def classify_status(text: str) -> str:
    """Return the most specific matching status label for a block of text."""
    lower = text.lower()
    for keywords, label, _ in STATUS_RULES:
        if any(kw in lower for kw in keywords):
            return label
    return "Unknown / No Change Detected"


# ── Congress.gov Scraper ───────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; S3971-Monitor/1.0; "
        "+https://github.com/your-org/s3971-monitor)"
    )
}


def fetch_bill_data() -> dict:
    """
    Scrape Congress.gov for S.3971 status info.
    Returns dict with keys: status_text, latest_action, latest_action_date, url
    """
    result = {
        "status_text": "Unknown",
        "latest_action": "Could not retrieve",
        "latest_action_date": "Unknown",
        "url": BILL_URL,
        "raw_status": "",
    }

    try:
        resp = requests.get(BILL_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # ── Latest Action ──────────────────────────────────────────────────────
        # Congress.gov puts "Latest Action:" in a <div class="overview-item-info">
        for item in soup.select("div.overview-item-info, div.item-info"):
            label = item.find("strong")
            if label and "latest action" in label.get_text(strip=True).lower():
                action_text = item.get_text(separator=" ", strip=True)
                # Strip the label itself
                action_text = action_text.replace(label.get_text(strip=True), "").strip()
                result["latest_action"] = action_text
                result["raw_status"] = action_text
                break

        # Fallback: look for the tracker/progress bar text
        if result["raw_status"] == "":
            tracker = soup.select_one("ol.bill-status, ul.bill-progress, div.bill-status")
            if tracker:
                result["raw_status"] = tracker.get_text(separator=" ", strip=True)

        # ── Bill Status heading ────────────────────────────────────────────────
        status_el = soup.select_one("span.bill-status-stage, div.status-stage, .bill-status")
        if status_el:
            result["status_text"] = status_el.get_text(strip=True)
        else:
            result["status_text"] = classify_status(result["raw_status"])

        # ── Date ───────────────────────────────────────────────────────────────
        # Look for date inside the latest-action block
        date_el = soup.select_one("span.action-date, span.date, time")
        if date_el:
            result["latest_action_date"] = date_el.get_text(strip=True)

        log.info("Fetched bill data: status=%s | action=%s",
                 result["status_text"], result["latest_action"][:80])

    except Exception as exc:
        log.error("Failed to fetch bill data: %s", exc)
        result["status_text"] = "Fetch Error"
        result["latest_action"] = str(exc)

    return result


# ── State Persistence ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"status": None, "last_checked": None, "last_action": None}


def save_state(status: str, action: str) -> None:
    STATE_FILE.write_text(json.dumps({
        "status": status,
        "last_action": action,
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }, indent=2))


# ── AI Summary (Anthropic API) ─────────────────────────────────────────────────

def generate_summary(bill_data: dict, changed: bool) -> str:
    """
    Call the Anthropic API to produce a 1–3 sentence human-readable summary.
    Falls back to a templated summary if the API key isn't set.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        change_note = "The status has changed since the last check." if changed else "No change from the previous check."
        return (
            f"S.3971 (SBIR/STTR Reauthorization Act) current status: "
            f"{bill_data['status_text']}. "
            f"Latest action: {bill_data['latest_action'][:120]}. "
            f"{change_note}"
        )

    prompt = (
        "You are a concise legislative analyst. Write 1–3 sentences summarizing "
        "the current status of S.3971, the SBIR/STTR Reauthorization Act, based "
        "on the following data. Be factual and clear.\n\n"
        f"Status: {bill_data['status_text']}\n"
        f"Latest Action: {bill_data['latest_action']}\n"
        f"Date: {bill_data['latest_action_date']}\n"
        f"Status changed: {'Yes' if changed else 'No'}"
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 200,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()
    except Exception as exc:
        log.warning("AI summary failed: %s", exc)
        return f"S.3971 status: {bill_data['status_text']}. Latest: {bill_data['latest_action'][:120]}."


# ── Email Sender ───────────────────────────────────────────────────────────────

def build_email_html(bill_data: dict, changed: bool, prev_status: str | None, summary: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    change_badge = (
        '<span style="background:#16a34a;color:#fff;padding:3px 10px;border-radius:12px;font-size:12px;">⚡ STATUS CHANGED</span>'
        if changed else
        '<span style="background:#6b7280;color:#fff;padding:3px 10px;border-radius:12px;font-size:12px;">No Change</span>'
    )
    prev_row = (
        f'<tr><td style="color:#6b7280;padding:4px 0;">Previous Status</td>'
        f'<td style="padding:4px 0 4px 16px;">{prev_status or "N/A"}</td></tr>'
        if prev_status else ""
    )
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:Georgia,serif;max-width:620px;margin:0 auto;padding:24px;background:#f9f9f7;color:#1a1a1a;">
  <div style="background:#1e3a5f;color:#fff;padding:24px 28px;border-radius:8px 8px 0 0;">
    <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;opacity:.7;margin-bottom:6px;">Legislative Monitor</div>
    <h1 style="margin:0;font-size:22px;font-weight:700;">S.3971 · SBIR/STTR Reauthorization Act</h1>
    <div style="margin-top:10px;">{change_badge}</div>
  </div>

  <div style="background:#fff;padding:24px 28px;border:1px solid #e5e5e3;border-top:none;">

    <p style="font-size:15px;line-height:1.6;color:#374151;margin-top:0;">{summary}</p>

    <table style="width:100%;border-collapse:collapse;font-size:14px;margin-top:20px;">
      <tr style="border-bottom:1px solid #e5e5e3;">
        <td style="color:#6b7280;padding:8px 0;width:160px;">Current Status</td>
        <td style="padding:8px 0 8px 16px;font-weight:600;color:#1e3a5f;">{bill_data['status_text']}</td>
      </tr>
      {prev_row}
      <tr style="border-bottom:1px solid #e5e5e3;">
        <td style="color:#6b7280;padding:8px 0;">Latest Action</td>
        <td style="padding:8px 0 8px 16px;">{bill_data['latest_action']}</td>
      </tr>
      <tr style="border-bottom:1px solid #e5e5e3;">
        <td style="color:#6b7280;padding:8px 0;">Action Date</td>
        <td style="padding:8px 0 8px 16px;">{bill_data['latest_action_date']}</td>
      </tr>
      <tr>
        <td style="color:#6b7280;padding:8px 0;">Checked At</td>
        <td style="padding:8px 0 8px 16px;">{ts}</td>
      </tr>
    </table>

    <div style="margin-top:24px;">
      <a href="{BILL_URL}"
         style="display:inline-block;background:#1e3a5f;color:#fff;text-decoration:none;
                padding:10px 20px;border-radius:6px;font-size:14px;">
        View on Congress.gov →
      </a>
    </div>
  </div>

  <div style="padding:14px 28px;font-size:11px;color:#9ca3af;background:#f9f9f7;border:1px solid #e5e5e3;border-top:none;border-radius:0 0 8px 8px;">
    Automated daily monitor · S.3971 (118th Congress) · 
    <a href="{BILL_URL}" style="color:#9ca3af;">Congress.gov source</a>
  </div>
</body>
</html>
"""


def send_email(subject: str, html_body: str) -> bool:
    if not SENDER_EMAIL or not GMAIL_APP_PASS:
        log.error("GMAIL_SENDER or GMAIL_APP_PASSWORD not set — skipping email.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, GMAIL_APP_PASS)
            server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
        log.info("Email sent → %s", RECIPIENT_EMAIL)
        return True
    except Exception as exc:
        log.error("Failed to send email: %s", exc)
        return False


# ── Main Orchestrator ──────────────────────────────────────────────────────────

def run() -> None:
    log.info("=== S.3971 Monitor starting ===")

    # 1. Load previous state
    state = load_state()
    prev_status = state.get("status")

    # 2. Fetch current bill data
    bill_data = fetch_bill_data()

    # Derive a clean status label from the scraped text
    classified = classify_status(
        bill_data["raw_status"] + " " + bill_data["status_text"] + " " + bill_data["latest_action"]
    )
    # Prefer explicit status_text from the page if it's not generic
    current_status = (
        bill_data["status_text"]
        if bill_data["status_text"] not in ("Unknown", "Unknown / No Change Detected", "Fetch Error")
        else classified
    )

    # 3. Detect change
    changed = (prev_status is None) or (current_status != prev_status)
    is_critical = current_status in CRITICAL_STATUSES

    log.info("Previous: %s | Current: %s | Changed: %s | Critical: %s",
             prev_status, current_status, changed, is_critical)

    # 4. Decide whether to send email
    force_daily = os.environ.get("FORCE_DAILY_EMAIL", "false").lower() == "true"
    should_email = changed or is_critical or force_daily

    if should_email:
        # 5. Generate AI summary
        summary = generate_summary(bill_data, changed)

        # 6. Build subject line
        if is_critical:
            subject = f"🚨 ALERT: S.3971 — {current_status}"
        elif changed:
            subject = f"📋 UPDATE: S.3971 status changed → {current_status}"
        else:
            subject = f"📋 Daily Check: S.3971 — {current_status} (no change)"

        # 7. Send email
        html = build_email_html(bill_data, changed, prev_status, summary)
        send_email(subject, html)
    else:
        log.info("No change and not a critical milestone — skipping email.")

    # 8. Persist updated state
    save_state(current_status, bill_data["latest_action"])
    log.info("=== Done. State saved. ===")


if __name__ == "__main__":
    run()
