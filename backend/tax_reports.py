"""Read-only annual exports of CONFIRMED real activity owned by a Privy DID.

No public insertion route exists. Simulation data must never enter this ledger.
All values stay in their recorded units; no invented CHF valuations or tax totals.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO, StringIO
from xml.sax.saxutils import escape
import logging

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row

from .storage import _db_url, _ensure_schema, _prepare_rows, _safe_did

LOG = logging.getLogger("mininex.tax")
FIELDS = (
    "date_utc", "source", "type", "asset", "quantity", "amount_usd",
    "fee_usd", "realized_net_usd", "network", "tx_hash", "record_id",
    "verification",
)


def validate_year(year: object) -> int:
    if type(year) is not int or not 2020 <= year <= datetime.now(timezone.utc).year:
        raise ValueError("invalid report year")
    return year


def list_verified_activity(did: str, year: int) -> list[dict]:
    """Fetch ONLY verified rows owned by the authenticated account and year."""
    did = _safe_did(did)
    year = validate_year(year)
    try:
        url = _db_url()
        _ensure_schema(url)
        with psycopg.connect(url, connect_timeout=6) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                _prepare_rows(cur, did)
                cur.execute(
                    """SELECT source, record_id, asset, activity_type, occurred_at,
                              quantity, amount_usd, fee_usd, realized_net_usd,
                              tx_hash, network, verification
                       FROM mininex_verified_activity
                       WHERE privy_did=%s AND verification='CONFIRMED'
                         AND occurred_at >= %s AND occurred_at < %s
                       ORDER BY occurred_at ASC,record_id ASC""",
                    (did, datetime(year, 1, 1, tzinfo=timezone.utc),
                     datetime(year + 1, 1, 1, tzinfo=timezone.utc)),
                )
                rows = cur.fetchall()
        return [
            {
                "date_utc": row["occurred_at"].astimezone(timezone.utc).isoformat(),
                "source": row["source"],
                "type": row["activity_type"],
                "asset": row["asset"],
                "quantity": str(row["quantity"]),
                "amount_usd": str(row["amount_usd"]) if row["amount_usd"] is not None else "",
                "fee_usd": str(row["fee_usd"]) if row["fee_usd"] is not None else "",
                "realized_net_usd": str(row["realized_net_usd"]) if row["realized_net_usd"] is not None else "",
                "network": row["network"] or "",
                "tx_hash": row["tx_hash"] or "",
                "record_id": row["record_id"],
                "verification": "CONFIRMED",
            }
            for row in rows
        ]
    except psycopg.Error:
        LOG.warning("MiniNex verified activity storage unavailable")
        raise HTTPException(503, "Verified activity unavailable") from None


def make_csv(rows: list[dict], year: int) -> bytes:
    validate_year(year)
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        # Prevent spreadsheet formula injection from untrusted explorer metadata.
        sanitized = {}
        for key in FIELDS:
            value = str(row.get(key, ""))
            if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                value = "'" + value
            sanitized[key] = value
        writer.writerow(sanitized)
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def make_pdf(rows: list[dict], year: int) -> bytes:
    """Compact multi-page PDF; report is supporting evidence, NOT a tax assessment."""
    validate_year(year)
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, LongTable
    from reportlab.lib.units import mm

    output = BytesIO()
    doc = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=18*mm, rightMargin=18*mm,
        topMargin=17*mm, bottomMargin=17*mm, title=f"MiniNex activity {year}",
        author="MiniNex",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="MiniHeading", parent=styles["Heading1"],
                              fontSize=19, leading=24, textColor=colors.HexColor("#073e38"),
                              spaceAfter=12))
    styles.add(ParagraphStyle(name="MiniSmall", parent=styles["Normal"],
                              fontSize=8.5, leading=12, spaceAfter=8))
    styles.add(ParagraphStyle(name="MiniCell", parent=styles["Normal"],
                              fontSize=7, leading=9))
    styles.add(ParagraphStyle(name="MiniCellRight", parent=styles["MiniCell"], alignment=TA_RIGHT))
    story = [
        Paragraph(f"MiniNex | Activity report {year}", styles["MiniHeading"]),
        Paragraph("Confirmed blockchain transfers and MiniNex trades, if available. "
                  "All timestamps UTC; figures are in recorded asset units or USD. "
                  "Missing market values are intentionally left blank.", styles["MiniSmall"]),
        Paragraph("Not a complete tax return. CHF tax valuations, year-end holdings, "
                  "cost-basis verification and external wallet activity must be reconciled "
                  "before filing.", styles["MiniSmall"]),
        Spacer(1, 5*mm),
        Paragraph(f"Confirmed records: {len(rows)}", styles["MiniSmall"]),
    ]
    if not rows:
        story.append(Paragraph(
            "No confirmed transactions are currently recorded for this year. "
            "Simulated trades are excluded.", styles["MiniSmall"]))
    else:
        headers=["UTC date","Source","Type","Asset","Quantity","Value USD","Fee USD"]
        data=[[Paragraph(h, styles["MiniCell"]) for h in headers]]
        for r in rows:
            date = str(r.get("date_utc", ""))[:16].replace("T", " ")
            values=[date,r.get("source",""),r.get("type",""),r.get("asset",""),
                    r.get("quantity",""),r.get("amount_usd",""),r.get("fee_usd","")]
            data.append([Paragraph(escape(str(v)), styles["MiniCellRight"] if k>=4 else styles["MiniCell"])
                         for k,v in enumerate(values)])
        table=LongTable(data, colWidths=[31*mm,28*mm,21*mm,15*mm,31*mm,27*mm,22*mm],
                        repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#d9f6e8")),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("BOTTOMPADDING",(0,0),(-1,-1),6),
            ("TOPPADDING",(0,0),(-1,-1),6),
            ("LINEBELOW",(0,0),(-1,0),0.6,colors.HexColor("#549785")),
            ("LINEBELOW",(0,1),(-1,-1),0.2,colors.HexColor("#d2e0dc")),
        ]))
        story.append(table)
        story.append(Spacer(1,5*mm))
        story.append(Paragraph(
            "The full machine-readable CSV contains transaction IDs, network, "
            "realized result and verification status.",styles["MiniSmall"]))
    doc.build(story)
    return output.getvalue()
