"""
notify_digest.py — sends an HTML email summary of the latest weekly digest.

Requires these environment variables (set as GitHub Actions secrets):
  SMTP_HOST        e.g. smtp.gmail.com
  SMTP_PORT        e.g. 587  (default)
  SMTP_USER        sender email / login
  SMTP_PASSWORD    app password or API key
  RECIPIENT_EMAIL  destination (default: christophe.carugati@digital-competition.com)

Usage:
  python notify_digest.py --digest data/digests/latest.json \\
         --dashboard-url https://christophecarugati-dc.github.io/ecn-pulse/digest/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger("notify-digest")

DEFAULT_RECIPIENT = "christophe.carugati@digital-competition.com"


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _score_color(score: int | None) -> str:
    if score == 5:
        return "#dc2626"
    if score == 4:
        return "#d97706"
    if score == 3:
        return "#2563eb"
    return "#6b7280"


def _urgency_color(urgency: str) -> str:
    return {"immediate": "#dc2626", "medium_term": "#d97706", "watch": "#16a34a"}.get(urgency, "#6b7280")


def build_html(digest: dict, dashboard_url: str) -> str:
    syn = digest.get("synthesis", {})
    items = digest.get("items", [])
    week = digest.get("week", "")
    generated = digest.get("generated_at", "")[:10]

    headline = syn.get("headline", f"Digital Competition Digest — {week}")
    exec_summary = syn.get("executive_summary", "")
    themes = syn.get("key_themes", [])
    connections = syn.get("connections", [])
    implications = syn.get("policy_implications", [])
    ai_enabled = syn.get("ai_enabled", False)

    # Top-N high-relevance items
    high_items = sorted(
        [it for it in items if it.get("_analysis", {}).get("relevance_score", 0) >= 4],
        key=lambda x: x.get("_analysis", {}).get("relevance_score", 0),
        reverse=True,
    )[:8]

    def esc(s: str) -> str:
        if not s:
            return ""
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    themes_html = ""
    for t in themes[:4]:
        themes_html += f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #e5e7eb;vertical-align:top;">
            <strong style="color:#1a1d23">{esc(t.get('theme', ''))}</strong><br>
            <span style="color:#6b7280;font-size:13px">{esc(t.get('description', ''))}</span>
          </td>
        </tr>"""

    items_html = ""
    for it in high_items:
        analysis = it.get("_analysis", {})
        score = analysis.get("relevance_score")
        title = it.get("title", "")
        url = it.get("url", "#")
        src_label = it.get("source_label", it.get("source", ""))
        summary = analysis.get("summary", it.get("abstract", "")[:200])
        relevance_exp = analysis.get("relevance_explanation", "")
        score_color = _score_color(score)

        items_html += f"""
        <tr>
          <td style="padding:12px 0;border-bottom:1px solid #e5e7eb;vertical-align:top;">
            <div style="margin-bottom:4px">
              <span style="font-size:10px;font-weight:700;text-transform:uppercase;
                color:{score_color};background:#fff8f8;border:1px solid {score_color};
                border-radius:3px;padding:1px 6px;margin-right:6px">{score}/5</span>
              <span style="font-size:10px;color:#6b7280;text-transform:uppercase;
                font-weight:600">{esc(src_label)}</span>
            </div>
            <a href="{esc(url)}" style="font-size:14px;font-weight:600;color:#1a1d23;
               text-decoration:none">{esc(title)}</a>
            {f'<p style="font-size:13px;color:#6b7280;margin:4px 0 0">{esc(summary)}</p>' if summary else ''}
            {f'<p style="font-size:12px;color:#6b7280;font-style:italic;margin:4px 0 0">→ {esc(relevance_exp)}</p>' if relevance_exp else ''}
          </td>
        </tr>"""

    implications_html = ""
    for imp in implications[:4]:
        urgency = imp.get("urgency", "watch")
        uc = _urgency_color(urgency)
        label = urgency.replace("_", " ").title()
        implications_html += f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #e5e7eb;vertical-align:top;">
            <span style="font-size:10px;font-weight:700;text-transform:uppercase;
              color:{uc};border:1px solid {uc};border-radius:3px;
              padding:1px 6px;margin-right:8px">{label}</span>
            <span style="font-size:13px;color:#374151">{esc(imp.get('implication', ''))}</span>
          </td>
        </tr>"""

    connections_html = ""
    for conn in connections[:3]:
        ct = (conn.get("connection_type") or "").replace("_", " ").title()
        connections_html += f"""
        <tr>
          <td style="padding:8px 0 8px 12px;border-bottom:1px solid #e5e7eb;
            border-left:3px solid #2563eb;vertical-align:top;">
            <span style="font-size:10px;font-weight:700;color:#6b7280;
              text-transform:uppercase">{esc(ct)}</span><br>
            <span style="font-size:13px;color:#374151">{esc(conn.get('description', ''))}</span>
          </td>
        </tr>"""

    ai_notice = (
        '<span style="font-size:12px;color:#2563eb;background:#eff6ff;'
        'border:1px solid #bfdbfe;border-radius:3px;padding:1px 8px">✦ AI-powered</span>'
        if ai_enabled else
        '<span style="font-size:12px;color:#92400e;background:#fffbeb;'
        'border:1px solid #fde68a;border-radius:3px;padding:1px 8px">Free mode (no AI)</span>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{esc(headline)}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:#f8f9fb;margin:0;padding:0">
  <table width="100%" cellpadding="0" cellspacing="0" bgcolor="#f8f9fb">
    <tr><td align="center" style="padding:24px 16px">

      <!-- Header -->
      <table width="620" cellpadding="0" cellspacing="0" bgcolor="#2563eb"
        style="border-radius:10px 10px 0 0">
        <tr>
          <td style="padding:20px 28px">
            <p style="color:#bfdbfe;font-size:12px;font-weight:700;
              text-transform:uppercase;letter-spacing:.6px;margin:0 0 4px">
              ECN Pulse · Weekly Digest · {esc(week)}
            </p>
            <p style="color:#ffffff;font-size:19px;font-weight:700;
              line-height:1.35;margin:0">{esc(headline)}</p>
            <p style="color:#bfdbfe;font-size:12px;margin:8px 0 0">
              Generated {esc(generated)} &nbsp;·&nbsp; {len(items)} items monitored
              &nbsp;·&nbsp; {ai_notice}
            </p>
          </td>
        </tr>
      </table>

      <!-- Body -->
      <table width="620" cellpadding="0" cellspacing="0" bgcolor="#ffffff"
        style="border:1px solid #e2e5eb;border-top:none">
        <tr>
          <td style="padding:24px 28px">

            <!-- Executive Summary -->
            <p style="font-size:13px;font-weight:700;text-transform:uppercase;
              letter-spacing:.5px;color:#6b7280;margin:0 0 10px">Executive Summary</p>
            <p style="font-size:14px;line-height:1.7;color:#1a1d23;margin:0 0 24px">
              {esc(exec_summary)}
            </p>

            {'<!-- Key Themes --><p style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#6b7280;margin:0 0 4px">Key Themes</p><table width="100%" cellpadding="0" cellspacing="0">' + themes_html + '</table><br>' if themes_html else ''}

            {'<!-- Top Items --><p style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#6b7280;margin:16px 0 4px">Highest-Relevance Publications</p><table width="100%" cellpadding="0" cellspacing="0">' + items_html + '</table><br>' if items_html else ''}

            {'<!-- Connections --><p style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#6b7280;margin:16px 0 4px">Cross-Publication Connections</p><table width="100%" cellpadding="0" cellspacing="0" style="border-left:none">' + connections_html + '</table><br>' if connections_html else ''}

            {'<!-- Implications --><p style="font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#6b7280;margin:16px 0 4px">Policy Implications</p><table width="100%" cellpadding="0" cellspacing="0">' + implications_html + '</table>' if implications_html else ''}

          </td>
        </tr>
      </table>

      <!-- Footer -->
      <table width="620" cellpadding="0" cellspacing="0" bgcolor="#f1f5f9"
        style="border:1px solid #e2e5eb;border-top:none;border-radius:0 0 10px 10px">
        <tr>
          <td style="padding:14px 28px;text-align:center">
            <a href="{esc(dashboard_url)}"
              style="display:inline-block;background:#2563eb;color:#fff;
                font-size:13px;font-weight:600;text-decoration:none;
                border-radius:6px;padding:8px 20px">
              View full digest →
            </a>
            <p style="font-size:11px;color:#9ca3af;margin:10px 0 0">
              ECN Pulse · Digital Competition Policy Monitor ·
              <a href="https://github.com/christophecarugati-dc/ecn-pulse"
                style="color:#9ca3af">GitHub</a>
            </p>
          </td>
        </tr>
      </table>

    </td></tr>
  </table>
</body>
</html>"""


def send_email(html: str, subject: str, recipient: str) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"ECN Pulse <{smtp_user}>"
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [recipient], msg.as_string())

    log.info("Email sent to %s", recipient)


def main() -> None:
    ap = argparse.ArgumentParser(description="Send weekly digest email")
    ap.add_argument("--digest", default="data/digests/latest.json")
    ap.add_argument("--dashboard-url", default="https://christophecarugati-dc.github.io/ecn-pulse/digest/")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        if not os.environ.get(var):
            log.error("Required environment variable %s is not set. Skipping email.", var)
            sys.exit(0)  # exit 0 so the workflow step doesn't fail

    try:
        digest = _load(args.digest)
    except Exception as exc:
        log.error("Could not load digest file %s: %s", args.digest, exc)
        sys.exit(1)

    syn = digest.get("synthesis", {})
    week = digest.get("week", "")
    headline = syn.get("headline", f"Digital Competition Digest — {week}")
    subject = f"[ECN Pulse] {headline}"

    recipient = os.environ.get("RECIPIENT_EMAIL", DEFAULT_RECIPIENT)
    html = build_html(digest, args.dashboard_url)
    send_email(html, subject, recipient)


if __name__ == "__main__":
    main()
