"""
In-memory CSV report generator and SMTP email delivery skill for Burgess Buddy.
Never writes temporary CSV files to disk.
"""

import csv
from datetime import datetime
from email.message import EmailMessage
import io
import logging
import os
import smtplib
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

CSV_COLUMNS = [
    "PONumber",
    "InvoiceNumber",
    "BuilderExpected",
    "BuilderScraped",
    "InspectionExpected",
    "TaskScraped",
    "FeeExpected",
    "FeeScraped",
    "Status",
    "ConfidenceScore",
    "Discrepancies",
    "AuditNotes",
]


def _format_currency(val: Any) -> str:
    """Formats numeric fee values as '$0.00' strings for CSV output."""
    if val is None or val == "":
        return ""
    if isinstance(val, (int, float)):
        return f"${float(val):.2f}"
    text = str(val).strip()
    if text.startswith("$"):
        return text
    try:
        return f"${float(text):.2f}"
    except ValueError:
        return text


def _extract_email_params(parameters: Optional[dict[str, Any]]) -> dict[str, Any]:
    """
    Extracts nested `Emailer` skill settings from `job["parameters"]` with
    backward-compatible fallback to top-level keys.
    """
    if not isinstance(parameters, dict):
        return {}
    nested = parameters.get("Emailer")
    if not isinstance(nested, dict):
        for k, v in parameters.items():
            if k.lower() == "emailer" and isinstance(v, dict):
                nested = v
                break
    merged = dict(parameters)
    if isinstance(nested, dict):
        merged.update(nested)
    return merged


def _parse_bool(val: Any, default: bool = True) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() not in ("false", "0", "no", "off")
    return bool(val)


def _normalize_row(item: dict[str, Any]) -> dict[str, str]:
    """Maps a reconciliation result dictionary into the standardized 12-column CSV schema."""
    discrepancies = item.get("Discrepancies") or item.get("discrepancies") or []
    if isinstance(discrepancies, list):
        discrepancies_str = "; ".join(str(d) for d in discrepancies if d)
    else:
        discrepancies_str = str(discrepancies)

    conf_val = item.get("ConfidenceScore") if "ConfidenceScore" in item else item.get("confidenceScore")
    conf_str = f"{float(conf_val):.2f}" if isinstance(conf_val, (int, float)) else str(conf_val or "")

    status = item.get("Status") or item.get("status")
    if not status:
        overall = item.get("overallMatch")
        if overall is True:
            status = "Reconciled"
        elif overall is False:
            status = "FlaggedForReview"
        else:
            status = "Processed"

    return {
        "PONumber": str(item.get("PONumber") or item.get("poNumber") or ""),
        "InvoiceNumber": str(item.get("InvoiceNumber") or item.get("invoiceNumber") or ""),
        "BuilderExpected": str(item.get("BuilderExpected") or item.get("builderExpected") or ""),
        "BuilderScraped": str(item.get("BuilderScraped") or item.get("builderScraped") or item.get("builder") or ""),
        "InspectionExpected": str(item.get("InspectionExpected") or item.get("inspectionExpected") or ""),
        "TaskScraped": str(item.get("TaskScraped") or item.get("taskScraped") or ""),
        "FeeExpected": _format_currency(item.get("FeeExpected") if "FeeExpected" in item else item.get("feeExpected")),
        "FeeScraped": _format_currency(item.get("FeeScraped") if "FeeScraped" in item else item.get("feeScraped")),
        "Status": str(status),
        "ConfidenceScore": conf_str,
        "Discrepancies": discrepancies_str,
        "AuditNotes": str(item.get("AuditNotes") or item.get("auditNotes") or ""),
    }


def build_reconciliation_csv(results: list[dict[str, Any]]) -> tuple[str, str]:
    """
    Generates an in-memory CSV string (`io.StringIO`) from reconciliation results.

    Returns:
        Tuple of (attachment_filename, csv_string).
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"supplypro_reconciliation_{date_str}.csv"

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()

    for item in results:
        writer.writerow(_normalize_row(item))

    return filename, buffer.getvalue()


def send_reconciliation_report(
    results: list[dict[str, Any]],
    telemetry: Optional[dict[str, Any]] = None,
    parameters: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
    job_name: Optional[str] = None,
) -> dict[str, Any]:
    """
    Generates the in-memory SupplyPro reconciliation CSV report and either:
      - Logs the email details, token/cost summary, and CSV table to the console
        when `dry_run=True` or SMTP credentials are not configured.
      - Sends the email with the CSV attachment via SMTP when `dry_run=False` and
        SMTP credentials are present.
    """
    email_params = _extract_email_params(parameters)
    telem = telemetry or {}

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com"
    smtp_port_raw = os.getenv("SMTP_PORT", "587").strip()
    smtp_port = int(smtp_port_raw) if smtp_port_raw.isdigit() else 587
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()

    recipient_email = str(
        email_params.get("recipientEmail")
        or email_params.get("recipient_email")
        or email_params.get("email")
        or os.getenv("REPORT_RECIPIENT_EMAIL", "")
    ).strip()

    attach_csv = _parse_bool(
        email_params.get("attachCsv") if "attachCsv" in email_params else email_params.get("attach_csv"),
        default=True,
    )

    filename, csv_content = build_reconciliation_csv(results)
    date_str = datetime.now().strftime("%Y-%m-%d")
    mode_tag = "[DRY-RUN] " if dry_run else ""
    title_label = job_name or "SupplyPro PortalSync"
    subject = f"{mode_tag}Burgess Buddy Reconciliation Report - {title_label} ({date_str})"

    total_orders = len(results)
    matched_orders = sum(1 for r in results if r.get("overallMatch") is True)
    mismatched_orders = total_orders - matched_orders

    input_tokens = int(telem.get("input_tokens", 0))
    output_tokens = int(telem.get("output_tokens", 0))
    total_tokens = int(telem.get("total_tokens", input_tokens + output_tokens))
    estimated_cost = float(telem.get("estimated_cost_usd", 0.0))

    attachment_note = (
        f"Please see the attached CSV report ({filename}) for full order-level details.\n"
        if attach_csv
        else "CSV attachment was disabled via job configuration (attachCsv=False).\n"
    )

    body_text = (
        f"Burgess Buddy Automated Reconciliation Summary\n"
        f"==============================================\n"
        f"Job             : {title_label}\n"
        f"Date            : {date_str}\n"
        f"Execution Mode  : {'DRY-RUN (Simulation)' if dry_run else 'LIVE'}\n"
        f"Orders Audited  : {total_orders} (Reconciled: {matched_orders}, FlaggedForReview: {mismatched_orders})\n\n"
        f"Gemini 2.5 Flash Telemetry & Cost\n"
        f"---------------------------------\n"
        f"Input Tokens    : {input_tokens:,} ($0.30 / 1M)\n"
        f"Output Tokens   : {output_tokens:,} ($2.50 / 1M)\n"
        f"Total Tokens    : {total_tokens:,}\n"
        f"Estimated Cost  : ${estimated_cost:.6f} USD\n\n"
        f"{attachment_note}"
    )

    if dry_run or not smtp_username or not smtp_password:
        reason = "dry_run=True" if dry_run else "SMTP_USERNAME/SMTP_PASSWORD not configured"
        divider = "=" * 80
        print(f"\n{divider}")
        print(f"📧 EMAILER SKILL PREVIEW (Skipped live SMTP send: {reason})")
        print(divider)
        print(f"   • To         : {recipient_email or '(not configured - set REPORT_RECIPIENT_EMAIL or recipientEmail)'}")
        print(f"   • From       : {smtp_username or 'buddy@burgess-inc.com (stub)'}")
        print(f"   • SMTP Server: {smtp_host}:{smtp_port}")
        print(f"   • Subject    : {subject}")
        if attach_csv:
            print(f"   • Attachment : {filename} ({len(csv_content.encode('utf-8'))} bytes in-memory)")
        else:
            print("   • Attachment : Disabled (attachCsv=False)")
        print(f"   • Telemetry  : {input_tokens:,} in / {output_tokens:,} out ({total_tokens:,} total) | Est. Cost: ${estimated_cost:.6f} USD")
        if attach_csv:
            print("\n📊 In-Memory CSV Attachment Preview:")
            print("-" * 80)
            print(csv_content.rstrip())
        print(divider + "\n")
        logger.info(
            "Emailer preview logged to console (%s): attachment=%s (attachCsv=%s), rows=%d",
            reason,
            filename,
            attach_csv,
            total_orders,
        )
        return {
            "sent": False,
            "mode": "console_preview",
            "reason": reason,
            "recipient": recipient_email,
            "subject": subject,
            "attachment_filename": filename if attach_csv else None,
            "attach_csv": attach_csv,
            "csv_rows": total_orders,
        }

    if not recipient_email:
        logger.warning(
            "SMTP credentials are set, but no recipient email was provided "
            "(set REPORT_RECIPIENT_EMAIL in .env or recipientEmail in job parameters). "
            "Logging CSV preview to console instead."
        )
        print(csv_content)
        return {
            "sent": False,
            "mode": "missing_recipient",
            "recipient": "",
            "subject": subject,
            "attachment_filename": filename if attach_csv else None,
            "attach_csv": attach_csv,
            "csv_rows": total_orders,
        }

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_username
    msg["To"] = recipient_email
    msg.set_content(body_text)

    if attach_csv:
        csv_bytes = csv_content.encode("utf-8")
        msg.add_attachment(
            csv_bytes,
            maintype="text",
            subtype="csv",
            filename=filename,
        )

    logger.info(
        "Sending reconciliation email to %s via %s:%s (attachCsv=%s, attachment=%s)...",
        recipient_email,
        smtp_host,
        smtp_port,
        attach_csv,
        filename if attach_csv else "None",
    )
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_username, smtp_password)
        server.send_message(msg)

    logger.info("Reconciliation email successfully delivered to %s.", recipient_email)
    return {
        "sent": True,
        "mode": "smtp",
        "recipient": recipient_email,
        "subject": subject,
        "attachment_filename": filename if attach_csv else None,
        "attach_csv": attach_csv,
        "csv_rows": total_orders,
    }
