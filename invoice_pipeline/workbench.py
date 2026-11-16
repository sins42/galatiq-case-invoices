"""The Invoice Review Workbench: a self-contained HTML page for the AP clerk.

`build_view_model` turns a batch of PipelineState into a plain dict (the data contract in the
design handoff). `render` turns that dict into one HTML file with no external assets beyond the
Google fonts. The page is rendered server-side so it is complete at rest; the inline JavaScript
only handles selection, filtering, and, in static mode, a simulated decision.
"""

from __future__ import annotations

import datetime as dt
import html
import json
from collections import Counter
from pathlib import Path

from .config import Settings
from .extraction import source_text
from .metrics import Metrics
from .models import InvoiceData, Outcome
from .normalize import is_relative_date, item_key, parse_date
from .payment import amount_paid
from .state import PipelineState
from .validation import Catalog, load_catalog, resolve_item

FRAUD_CODES = {"ZERO_STOCK", "UNKNOWN_VENDOR", "RELATIVE_DUE_DATE", "PRESSURE_LANGUAGE"}

# Finding code -> flag-chip category used by the queue filter.
FLAG_CATEGORY = {
    "OVERSTOCK": "overstock",
    "ZERO_STOCK": "fraud",
    "PRESSURE_LANGUAGE": "fraud",
    "RELATIVE_DUE_DATE": "fraud",
    "UNKNOWN_ITEM": "unknown_item",
    "UNKNOWN_VENDOR": "unknown_vendor",
    "PRICE_ABOVE_CATALOG": "price",
    "TOTAL_OVERCHARGE": "price",
    "REVISION_OF_PRIOR": "revision",
    "FOREIGN_CURRENCY": "currency",
}


def _m(value: float | None) -> str:
    return f"{value:,.2f}" if value is not None else "-"


def _e(value: object) -> str:
    return html.escape(str(value)) if value is not None else ""


# ---------------------------------------------------------------------------
# View model
# ---------------------------------------------------------------------------

def _owner(total: float, fraud_signals: int, settings: Settings) -> str:
    if total >= settings.scrutiny_threshold or fraud_signals >= 2:
        return "Controller"
    return "AP clerk"


def _due(due_date: str | None, as_of: dt.date) -> tuple[int | None, bool, str, str]:
    """Return (due_days, relative, group, label). group is overdue|soon|later."""
    if is_relative_date(due_date):
        return None, True, "overdue", f'Due “{due_date}”'
    parsed = parse_date(due_date)
    if parsed is None:
        return None, False, "later", "No due date" if not due_date else f"Due {_e(due_date)}"
    days = (parsed - as_of).days
    label = f"Due {parsed.strftime('%b %d').replace(' 0', ' ')}"
    if days < 0:
        return days, False, "overdue", f"{abs(days)}d overdue"
    if days == 0:
        return days, False, "soon", "Due today"
    if days <= 3:
        return days, False, "soon", f"Due in {days}d"
    return days, False, "later", label


def _stock_note(name: str, demand: dict, contrib: dict, zero: bool, catalog: Catalog) -> str:
    total = demand.get(name, 0)
    stock = 0 if zero else catalog[item_key(name)].stock
    lines = contrib.get(name, [])
    prefix = f"lines {', '.join(map(str, lines))}: " if len(lines) > 1 else ""
    return f"{prefix}{total} billed · {stock} in stock"


def _line_rows(invoice: InvoiceData, catalog: Catalog, findings: list) -> list[dict]:
    demand: dict[str, int] = {}
    contrib: dict[str, list[int]] = {}
    for idx, li in enumerate(invoice.line_items, start=1):
        entry = resolve_item(li.name, catalog)
        if entry:
            demand[entry.name] = demand.get(entry.name, 0) + max(li.quantity or 0, 0)
            contrib.setdefault(entry.name, []).append(idx)

    codes_by_key: dict[str, set[str]] = {}
    for f in findings:
        if f.item:
            codes_by_key.setdefault(item_key(f.item), set()).add(f.code)

    rows = []
    for li in invoice.line_items:
        entry = resolve_item(li.name, catalog)
        codes = set(codes_by_key.get(item_key(li.name), set()))
        if entry:
            codes |= codes_by_key.get(item_key(entry.name), set())
        flag = note = None
        if "UNKNOWN_ITEM" in codes:
            flag, note = "review", "not in catalog"
        elif "INVALID_QUANTITY" in codes:
            flag, note = "review", "quantity not positive"
        elif "NEGATIVE_PRICE" in codes:
            flag, note = "review", "negative unit price"
        elif "ZERO_STOCK" in codes and entry:
            flag, note = "review", _stock_note(entry.name, demand, contrib, True, catalog)
        elif "OVERSTOCK" in codes and entry:
            flag, note = "review", _stock_note(entry.name, demand, contrib, False, catalog)
        elif "PRICE_ABOVE_CATALOG" in codes and entry and entry.unit_price is not None and li.unit_price is not None:
            flag, note = "warning", f"catalog {_m(entry.unit_price)} · billed {_m(li.unit_price)}"
        elif "LINE_AMOUNT_MISMATCH" in codes:
            flag, note = "warning", "line amount does not match qty × unit"
        rows.append(
            {"name": li.name, "quantity": li.quantity, "unit_price": li.unit_price,
             "amount": li.amount, "flag": flag, "note": note}
        )
    return rows


def _totals(invoice: InvoiceData, findings: list) -> tuple[list, str | None]:
    lines_sum = sum(
        (li.amount if li.amount is not None else (li.quantity or 0) * (li.unit_price or 0))
        for li in invoice.line_items
    )
    subtotal = invoice.subtotal if invoice.subtotal is not None else lines_sum
    rows: list = [["Subtotal", round(subtotal, 2)]]
    if invoice.tax is not None:
        label = f"Tax ({invoice.tax_rate * 100:g}%)" if invoice.tax_rate else "Tax"
        rows.append([label, round(invoice.tax, 2)])
    if invoice.shipping:
        rows.append(["Shipping", round(invoice.shipping, 2)])
    if invoice.other_fees:
        rows.append(["Other fees", round(invoice.other_fees, 2)])

    note = None
    if any(f.code == "TOTAL_OVERCHARGE" for f in findings):
        computed = lines_sum + (invoice.tax or 0) + (invoice.shipping or 0) + (invoice.other_fees or 0)
        gap = (invoice.total or 0) - computed
        note = f"{_m(gap)} above the summed lines plus tax"
    return rows, note


def _source_lines(src_text: str, invoice: InvoiceData, findings: list) -> list[dict]:
    needles: list[tuple[str, str]] = []  # (lowercased needle, severity)

    def add(value, severity="review"):
        if value:
            needles.append((str(value).lower(), severity))

    codes = {f.code for f in findings}
    for f in findings:
        if f.item and f.code in {"UNKNOWN_ITEM", "OVERSTOCK", "ZERO_STOCK", "INVALID_QUANTITY", "NEGATIVE_PRICE"}:
            add(f.item, "review")
        if f.item and f.code == "PRICE_ABOVE_CATALOG":
            add(f.item, "warning")
    if "UNKNOWN_VENDOR" in codes:
        add(invoice.vendor, "review")
    if "RELATIVE_DUE_DATE" in codes:
        add(invoice.due_date, "review")
    if "FOREIGN_CURRENCY" in codes:
        add(invoice.currency, "review")
    if "REVISION_OF_PRIOR" in codes:
        add(invoice.invoice_number, "review")
        add(invoice.revision, "review")
    if "TOTAL_OVERCHARGE" in codes and invoice.total is not None:
        add(_m(invoice.total), "warning")
    if "PRESSURE_LANGUAGE" in codes:
        for phrase in _pressure_phrases(invoice):
            add(phrase, "review")

    out = []
    for text in (src_text or "").split("\n"):
        low = text.lower()
        sev = None
        for needle, severity in needles:
            if needle and needle in low:
                sev = "review" if severity == "review" or sev == "review" else "warning"
        out.append({"text": text, "flag": sev})
    return out


def _pressure_phrases(invoice: InvoiceData) -> list[str]:
    """Pressure phrases that appear in the invoice free text (best-effort for highlighting)."""
    from .config import PRESSURE_PHRASES
    text = " ".join(p for p in [invoice.notes, invoice.payment_terms] if p).lower()
    return [p for p in PRESSURE_PHRASES if p in text]


def _short_summary(findings: list) -> str:
    c = Counter(f.code for f in findings)
    phrases: list[str] = []

    def add(text):
        if text not in phrases:
            phrases.append(text)

    if c["UNKNOWN_ITEM"]:
        add(f"{c['UNKNOWN_ITEM']} item{'s' if c['UNKNOWN_ITEM'] > 1 else ''} not in catalog")
    if c["OVERSTOCK"] or c["ZERO_STOCK"]:
        add("billed over stock")
    if c["UNKNOWN_VENDOR"]:
        add("vendor not approved")
    if c["PRICE_ABOVE_CATALOG"]:
        add("priced above catalog")
    if c["TOTAL_OVERCHARGE"]:
        add("total over line items")
    if c["REVISION_OF_PRIOR"]:
        add("revises an earlier invoice")
    if c["FOREIGN_CURRENCY"]:
        add("billed in foreign currency")
    if c["RELATIVE_DUE_DATE"]:
        add("vague due date")
    if c["PRESSURE_LANGUAGE"]:
        add("urgency language")
    if c["DUPLICATE_INVOICE"]:
        add("duplicate submission")
    if not phrases:
        add(findings[0].message[:48] if findings else "over the scrutiny threshold")
    return ", ".join(phrases[:2])


def _key(state: PipelineState) -> str:
    inv = state.invoice
    num = (inv.invoice_number if inv else None) or "NA"
    return f"{num}_{Path(state.invoice_path).name}".replace(" ", "_")


def _item(
    *, key: str, invoice: InvoiceData, findings: list, reasons: list[str], source_file: str,
    file_format: str | None, method: str | None, src_text: str, approval: str | None,
    assigned_to: str | None, settings: Settings, catalog: Catalog, as_of: dt.date, conn=None,
) -> dict:
    """Build one escalated-invoice queue item. Shared by the batch and ledger paths."""
    amount = invoice.total or 0
    fraud = sum(1 for f in findings if f.code in FRAUD_CODES)
    due_days, relative, group, due_label = _due(invoice.due_date, as_of)
    clean_reasons = [r for r in reasons if not r.startswith(("Reviewer:", "Routed to"))]
    routed = bool(assigned_to)
    # On a revision, show what was already paid so a reviewer approves only the difference.
    prior_paid = pay_difference = None
    if conn is not None and any(f.code == "REVISION_OF_PRIOR" for f in findings):
        already = amount_paid(conn, invoice)
        if already > 0:
            prior_paid = round(already, 2)
            pay_difference = round(amount - already, 2)
    return {
        "key": key,
        "invoice_number": invoice.invoice_number,
        "vendor": invoice.vendor or "(no vendor)",
        "source_file": source_file,
        "format": (file_format or "").upper(),
        "method": method,
        "total": amount,
        "prior_paid": prior_paid,
        "pay_difference": pay_difference,
        "currency": invoice.currency or "",
        "invoice_date": invoice.invoice_date,
        "due_date": invoice.due_date,
        "due_days": due_days,
        "due_relative": relative,
        "due_group": group,
        "due_label": due_label,
        "owner": "Controller" if routed else _owner(amount, fraud, settings),
        "routed": routed,
        "summary": _short_summary(findings),
        "why": " ".join(clean_reasons) if clean_reasons else "; ".join(f.message for f in findings),
        "findings": [
            {"code": f.code, "severity": f.severity.value, "item": f.item, "message": f.message}
            for f in findings
        ],
        "categories": sorted({FLAG_CATEGORY[f.code] for f in findings if f.code in FLAG_CATEGORY}),
        "fraud_signals": fraud,
        "approval": approval or "skipped",
        "lines": _line_rows(invoice, catalog, findings),
        **dict(zip(("totals", "total_note"), _totals(invoice, findings))),
        "source_lines": _source_lines(src_text, invoice, findings),
    }


def _assemble(recs: list[dict], queue: list[dict], run: dict) -> dict:
    order = {"overdue": 0, "soon": 1, "later": 2}
    queue.sort(key=lambda q: (order[q["due_group"]], q["due_days"] if q["due_days"] is not None else 10**6))
    sums = {o: [0, 0.0] for o in Outcome}
    billed = dup_n = 0
    dup_amt = 0.0
    for r in recs:
        sums[r["outcome"]][0] += 1
        sums[r["outcome"]][1] += r["amount"]
        billed += r["amount"]
        if r["dup"]:
            dup_n += 1
            dup_amt += r["amount"]
    run = {**run, "billed_total": round(billed, 2)}
    tile = lambda o: {"count": sums[o][0], "amount": round(sums[o][1], 2)}
    return {
        "run": run,
        "summary": {
            "paid": tile(Outcome.PAID),
            "rejected": tile(Outcome.REJECTED),
            "escalated": tile(Outcome.ESCALATED),
            "duplicates_blocked": {"count": dup_n, "amount": round(dup_amt, 2)},
            "urgent": [q["invoice_number"] for q in queue if q["due_group"] in ("overdue", "soon")],
        },
        "queue": queue,
    }


def build_view_model(
    states: list[PipelineState], metrics: Metrics, settings: Settings, catalog: Catalog, conn=None
) -> dict:
    as_of = settings.as_of_date or dt.date.today()
    recs, queue = [], []
    for state in states:
        inv = state.invoice
        if inv is None or state.outcome is None:
            continue
        recs.append({"outcome": state.outcome, "amount": inv.total or 0,
                     "dup": any(f.code == "DUPLICATE_INVOICE" for f in state.findings)})
        if state.outcome is Outcome.ESCALATED:
            queue.append(_item(
                key=_key(state), invoice=inv, findings=state.findings, reasons=state.reasons,
                source_file=Path(state.invoice_path).name, file_format=state.file_format, method=state.method,
                src_text=source_text(state.raw_content), approval="agent_escalated" if state.proposal else "skipped",
                assigned_to=None, settings=settings, catalog=catalog, as_of=as_of, conn=conn,
            ))
    run = {"id": states[0].run_id if states else "", "finished_at": dt.datetime.now().strftime("%a %b %d %Y %H:%M"),
           "file_count": len(recs)}
    return _assemble(recs, queue, run)


def build_view_model_from_ledger(conn, settings: Settings) -> dict:
    """The live view model: rebuilt from the ledger alone, with no PipelineState in hand."""
    from .models import Finding
    catalog = load_catalog(conn)
    as_of = settings.as_of_date or dt.date.today()
    recs, queue, last_run = [], [], ""
    for row in conn.execute("SELECT * FROM invoices_processed ORDER BY id"):
        if not row["invoice_json"]:
            continue
        invoice = InvoiceData.model_validate_json(row["invoice_json"])
        detail = json.loads(row["reasons"]) if row["reasons"] else {"reasons": [], "findings": []}
        findings = [Finding(**f) for f in detail.get("findings", [])]
        outcome = Outcome(row["status"])
        last_run = row["run_id"] or last_run
        recs.append({"outcome": outcome, "amount": row["total"] or 0,
                     "dup": any(f.code == "DUPLICATE_INVOICE" for f in findings)})
        if outcome is Outcome.ESCALATED:
            queue.append(_item(
                key=f'{row["number_key"] or "NA"}_{row["source_file"]}', invoice=invoice, findings=findings,
                reasons=detail.get("reasons", []), source_file=row["source_file"], file_format=row["file_format"],
                method=row["method"], src_text=row["source_text"] or "", approval=row["approval"],
                assigned_to=row["assigned_to"], settings=settings, catalog=catalog, as_of=as_of, conn=conn,
            ))
    run = {"id": last_run or "ledger", "finished_at": dt.datetime.now().strftime("%a %b %d %Y %H:%M"),
           "file_count": len(recs)}
    return _assemble(recs, queue, run)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_FORMAT_METHOD = {"model": "Read by model · grounded against source", "structured": "Parsed directly from source"}
_METHOD_SHORT = {"model": "Model, grounded", "structured": "Parsed directly"}
_STATUS = {
    "escalated": ("review", "Needs your review"),
    "paid": ("paid", "Approved and paid"),
    "rejected": ("rej", "Rejected by reviewer"),
    "routed": ("accent", "Routed to Controller"),
}


def _flag_chip(f: dict) -> str:
    item = f' <b>{_e(f["item"])}</b>' if f.get("item") else ""
    return f'<span class="flag {_e(f["severity"])}" title="{_e(f["message"])}">{_e(f["code"])}{item}</span>'


def _due_chip(q: dict) -> str:
    tone = {"overdue": "rej", "soon": "review", "later": "muted"}[q["due_group"]]
    return f'<span class="due {tone}">{_e(q["due_label"])}</span>'


def _line_table(q: dict) -> str:
    head = ('<div class="lt-row lt-head"><span>Item</span><span class="num">Qty</span>'
            '<span class="num">Unit</span><span class="num">Amount</span></div>')
    body = ""
    for li in q["lines"]:
        cls = f" flag-{li['flag']}" if li["flag"] else ""
        note = f'<span class="lt-note">{_e(li["note"])}</span>' if li["note"] else ""
        body += (f'<div class="lt-row{cls}"><span class="lt-item">{_e(li["name"])}{note}</span>'
                 f'<span class="num">{_e(li["quantity"])}</span>'
                 f'<span class="num">{_m(li["unit_price"])}</span>'
                 f'<span class="num">{_m(li["amount"])}</span></div>')
    foot = ""
    for label, value in q["totals"]:
        foot += f'<div class="lt-row lt-foot"><span class="lt-flabel">{_e(label)}</span><span class="num">{_m(value)}</span></div>'
    tnote = f'<span class="lt-tnote">{_e(q["total_note"])}</span>' if q["total_note"] else ""
    cls = " flag-warning" if q["total_note"] else ""
    foot += (f'<div class="lt-row lt-total{cls}"><span class="lt-flabel">Total billed{tnote}</span>'
             f'<span class="num">{_m(q["total"])} <span class="cur">{_e(q["currency"])}</span></span></div>')
    return f'<div class="lt">{head}{body}{foot}</div>'


def _pipeline_strip(q: dict) -> str:
    review = sum(1 for f in q["findings"] if f["severity"] in ("review", "critical"))
    warn = sum(1 for f in q["findings"] if f["severity"] == "warning")
    if review:
        val_tone, val_text = "review", f"{review} flag{'s' if review > 1 else ''} need judgment"
    elif warn:
        val_tone, val_text = "warn", f"{warn} flag{'s' if warn > 1 else ''} · warning"
    else:
        val_tone, val_text = "muted", "No blocking flags"
    approval = ("review", "Agent + critic disagreed") if q["approval"] == "agent_escalated" else ("muted", "Skipped · escalated first")
    cells = [
        ("accent", "Ingested", f'{q["format"]} file'),
        ("accent", "Extracted", _METHOD_SHORT.get(q["method"], q["method"] or "")),
        (val_tone, "Validated", val_text),
        (approval[0], "Approval", approval[1]),
        ("review", "Payment", "Held · nothing paid"),
    ]
    inner = "".join(
        f'<div class="ps-cell"><span class="ps-dot {t}"></span>'
        f'<div><div class="ps-lab">{_e(lab)}</div><div class="ps-det">{_e(det)}</div></div></div>'
        for t, lab, det in cells
    )
    return f'<div class="pstrip">{inner}</div>'


def _fraud_banner(q: dict) -> str:
    if q["fraud_signals"] < 2:
        return ""
    signals = ", ".join(f["code"].replace("_", " ").lower() for f in q["findings"] if f["code"] in FRAUD_CODES)
    tail = " At this amount it also sits above the scrutiny threshold." if q["total"] >= 10000 else ""
    return (f'<div class="fraud"><span class="fraud-mark">!</span>'
            f'<div><b>{q["fraud_signals"]} fraud signals on one invoice.</b> {_e(signals)}.{tail}</div></div>')


def _detail(q: dict) -> str:
    tone, label = _STATUS["routed"] if q.get("routed") else _STATUS["escalated"]
    meta = f'{_e(q["invoice_number"])}'
    if q["invoice_date"] or q["due_date"]:
        meta += f' · {_e(q["invoice_date"] or "?")} → due {_e(q["due_date"] or "?")}'
    meta += f' · owner: {_e(q["owner"])}'
    flags = "".join(_flag_chip(f) for f in q["findings"])
    method_note = _FORMAT_METHOD.get(q["method"], "")
    revnote = ""
    if q.get("pay_difference") is not None:
        if q["pay_difference"] > 0:
            revnote = (f'<div class="revnote">Approving pays only the difference: {_m(q["pay_difference"])} '
                       f'{_e(q["currency"])} ({_m(q["prior_paid"])} already paid on this number).</div>')
        else:
            revnote = (f'<div class="revnote">{_m(q["prior_paid"])} already paid, at or above this total. '
                       f'Approving records it with no further payment.</div>')
    return f'''<div class="dv" data-key="{_e(q["key"])}" data-amt="{q["total"]}" data-number="{_e(q["invoice_number"])}">
  <div class="d-head">
    <div class="d-head-l">
      <div class="pill {tone}" data-pill><span class="dot"></span><span data-pill-text>{label}</span></div>
      <div class="d-vendor">{_e(q["vendor"])}</div>
      <div class="d-meta">{meta}</div>
    </div>
    <div class="d-head-r">
      <div class="d-amt">{_m(q["total"])} <span class="cur">{_e(q["currency"])}</span></div>
      {_due_chip(q)}
    </div>
  </div>
  <div class="d-body">
    {_fraud_banner(q)}
    {_pipeline_strip(q)}
    <div class="d-sec"><h3 class="sec-t">Why this needs you</h3>
      <div class="evidence"><p class="why">{_e(q["why"])}</p><div class="flags">{flags}</div></div></div>
    <div class="d-sec"><div class="sec-head"><h3 class="sec-t">Extracted line items</h3>
      <span class="sec-note">{_e(method_note)}</span></div>{_line_table(q)}</div>
  </div>
  <div class="decision" data-decision>
    <div class="dec-open">
      <div class="dec-labs"><span class="sec-t">Your decision</span>
        <span class="dec-hint">Recorded on the ledger with your name and reason</span></div>
      {revnote}
      <div class="presets">
        <button type="button" class="preset">Confirmed with vendor</button>
        <button type="button" class="preset">Stock arriving on open PO</button>
        <button type="button" class="preset">Vendor not approved</button>
        <button type="button" class="preset">Suspected fraud</button>
      </div>
      <div class="dec-row">
        <input class="reason" type="text" placeholder="Reason (required for reject or route)">
        <button type="button" class="btn" data-act="route">Route to Controller</button>
        <button type="button" class="btn reject" data-act="reject">Reject</button>
        <button type="button" class="btn approve" data-act="approve">Approve &amp; pay</button>
      </div>
    </div>
    <div class="dec-done"><span class="dot"></span><span class="dec-done-text"></span>
      <button type="button" class="undo">Undo</button></div>
  </div>
</div>'''


def _source(q: dict) -> str:
    lines = ""
    for i, ln in enumerate(q["source_lines"], start=1):
        cls = f" flag-{ln['flag']}" if ln["flag"] else ""
        lines += f'<div class="src-row{cls}"><span class="src-n">{i}</span><span class="src-t">{_e(ln["text"])}</span></div>'
    return f'''<div class="sv" data-key="{_e(q["key"])}">
  <div class="sv-head"><div><div class="sec-t">Source document</div>
    <div class="sv-file">{_e(q["source_file"])}</div></div>
    <span class="badge">{_e(q["format"])}</span></div>
  <div class="sv-body">{lines}</div>
  <div class="sv-legend"><span class="sw review"></span>Value behind a review flag
    <span class="sw warning"></span>Warning</div>
</div>'''


def _queue_group(title: str, tone: str, items: list[dict]) -> str:
    if not items:
        return ""
    amount = sum(q["total"] for q in items)
    rows = ""
    for q in items:
        owner_slug = "mine" if q["owner"] == "AP clerk" else "controller"
        tag = '<span class="qtag routed" data-tag>ROUTED</span>' if q.get("routed") else '<span class="qtag" data-tag></span>'
        rows += f'''<button type="button" class="qrow" data-key="{_e(q["key"])}" data-number="{_e(q["invoice_number"])}" data-owner="{owner_slug}" data-cats="{' '.join(q["categories"])}" aria-current="false">
      <span class="qcheck" data-check role="checkbox" aria-checked="false" tabindex="0"></span>
      <span class="qmain">
        <span class="qline1"><span class="qvendor">{_e(q["vendor"])}</span>{tag}</span>
        <span class="qline2"><span class="qnum">{_e(q["invoice_number"])}</span> · {_e(q["summary"])}</span>
        <span class="qline3">{_due_chip(q)}<span class="owner">{_e(q["owner"])}</span></span>
      </span>
      <span class="qright"><span class="qamt">{_m(q["total"])}</span><span class="qcur">{_e(q["currency"])}</span></span>
    </button>'''
    return f'''<div class="qgroup" data-group>
    <div class="qg-head"><span class="qg-t {tone}"><span class="gdot"></span>{_e(title)}</span>
      <span class="qg-n">{len(items)} · {_m(amount)}</span></div>{rows}</div>'''


def render(view_model: dict, live: bool = False) -> str:
    vm = view_model
    s = vm["summary"]
    run = vm["run"]
    paid, rej, esc = s["paid"], s["rejected"], s["escalated"]
    stopped = rej["count"] + esc["count"]  # held or rejected, i.e. not auto-paid
    urgent = vm["queue"]
    overdue_soon = [q for q in urgent if q["due_group"] in ("overdue", "soon")]

    groups = {"overdue": [], "soon": [], "later": []}
    for q in vm["queue"]:
        groups[q["due_group"]].append(q)
    queue_html = (
        _queue_group("Overdue", "rej", groups["overdue"])
        + _queue_group("Due within 3 days", "review", groups["soon"])
        + _queue_group("Later", "muted", groups["later"])
    )
    details = "".join(_detail(q) for q in vm["queue"])
    sources = "".join(_source(q) for q in vm["queue"])

    mine = sum(1 for q in vm["queue"] if q["owner"] == "AP clerk")
    controller = len(vm["queue"]) - mine
    urgent_list = ", ".join(s["urgent"][:3]) or "none"

    # outcome bar weights
    bar = (f'<span class="seg paid" style="flex-grow:{paid["count"] or 0.01}"></span>'
           f'<span class="seg rej" style="flex-grow:{rej["count"] or 0.01}"></span>'
           f'<span class="seg esc" style="flex-grow:{esc["count"] or 0.01}"></span>')

    js_state = json.dumps({
        "live": live,
        "held": esc["amount"], "open": esc["count"],
        "amounts": {q["key"]: q["total"] for q in vm["queue"]},
        "numbers": {q["key"]: q["invoice_number"] for q in vm["queue"]},
        "routed": {q["key"]: bool(q.get("routed")) for q in vm["queue"]},
        "keys": [q["key"] for q in vm["queue"]],
    })

    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invoice Review Workbench</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>{_CSS}</style></head>
<body><div class="page">
  <header class="header">
    <div class="h-left">
      <div class="brand"><span class="eyebrow">Acme · Accounts payable</span><h1>Run review</h1></div>
      <div class="tabs">
        <button class="tab active">Review queue <span class="tab-n" id="tab-open">{esc["count"]}</span></button>
        <button class="tab" disabled>All invoices <span class="tab-n">{run["file_count"]}</span></button>
        <button class="tab" disabled>Run impact</button>
      </div>
    </div>
    <div class="h-right">run {_e(run["id"])} · {_e(run["finished_at"])} · {run["file_count"]} files</div>
  </header>

  <section class="strip">
    <div class="tile">
      <div class="tile-top"><span class="tile-k">Outcome of {run["file_count"]} invoices</span>
        <span class="tile-billed">${_m(run["billed_total"])} billed</span></div>
      <div class="bar">{bar}</div>
      <div class="bar-legend"><span><span class="d paid"></span>{paid["count"]} paid</span>
        <span><span class="d rej"></span>{rej["count"]} auto-rejected</span>
        <span><span class="d esc"></span>{esc["count"]} escalated</span></div>
    </div>
    <div class="tile accent-l review">
      <div class="tile-k">Waiting on you</div>
      <div class="tile-v review" id="open-count">{esc["count"]}</div>
      <div class="tile-sub mono" id="held-amt">${_m(esc["amount"])} held</div>
    </div>
    <div class="tile accent-l rej">
      <div class="tile-k">Overdue or due ≤ 3d</div>
      <div class="tile-v rej">{len(overdue_soon)}</div>
      <div class="tile-sub">{_e(urgent_list)}</div>
    </div>
    <div class="tile">
      <div class="tile-k">Stopped before payment</div>
      <div class="tile-v">{stopped} <span class="tile-of">of {run["file_count"]}</span></div>
      <div class="tile-sub">{s["duplicates_blocked"]["count"]} duplicates · ${_m(s["duplicates_blocked"]["amount"])}</div>
    </div>
    <div class="tile">
      <div class="tile-k">Paid, no human touch</div>
      <div class="tile-v paid">{paid["count"]}</div>
      <div class="tile-sub mono">${_m(paid["amount"])} · same run</div>
    </div>
  </section>

  <section class="workspace">
    <div class="pane queue">
      <div class="q-controls">
        <div class="seg-ctl" role="group" aria-label="Filter by owner">
          <button class="seg-btn active" data-owner="all">All <span class="seg-n">{len(vm["queue"])}</span></button>
          <button class="seg-btn" data-owner="mine">Mine <span class="seg-n">{mine}</span></button>
          <button class="seg-btn" data-owner="controller">Controller <span class="seg-n">{controller}</span></button>
        </div>
        <div class="chips">
          <button class="chip active" data-cat="all">All flags</button>
          <button class="chip fraud-chip" data-cat="fraud">Fraud signals</button>
          <button class="chip" data-cat="overstock">Over stock</button>
          <button class="chip" data-cat="unknown_vendor">Unknown vendor</button>
          <button class="chip" data-cat="unknown_item">Unknown item</button>
          <button class="chip" data-cat="price">Price &amp; totals</button>
          <button class="chip" data-cat="revision">Revision</button>
          <button class="chip" data-cat="currency">Currency</button>
        </div>
      </div>
      <div class="bulk" data-bulk hidden>
        <span class="bulk-n" data-bulk-n>0 selected</span>
        <button class="bulk-btn route" data-bulk-act="route">Route to Controller</button>
        <button class="bulk-btn reject" data-bulk-act="reject">Reject</button>
        <button class="bulk-btn clear" data-bulk-act="clear">Clear</button>
      </div>
      <div class="q-scroll">{queue_html}</div>
    </div>
    <div class="pane detail" id="detail">{details}
      <div class="dv-empty">Select an invoice from the queue.</div></div>
    <div class="pane source" id="source">{sources}</div>
  </section>

  <p class="preview-note">{"Live: Approve, Reject and Route write each decision to the ledger." if live else "Preview: decisions here are simulated in the browser. The live workbench (python main.py --serve) writes each decision to the ledger."}</p>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script type="application/json" id="vm">{js_state}</script>
<script>{_JS}</script>
</body></html>'''


def render_report(states: list[PipelineState], metrics: Metrics, settings: Settings, catalog: Catalog, conn=None) -> str:
    return render(build_view_model(states, metrics, settings, catalog, conn=conn))


def write(path: Path, states: list[PipelineState], metrics: Metrics, settings: Settings, conn) -> Path:
    catalog = load_catalog(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(states, metrics, settings, catalog, conn=conn), encoding="utf-8")
    return path


_CSS = r"""
:root{
  --bg:#f3f5f9; --surface:#fff; --surface-2:#f6f8fb; --sel:#eef2ff;
  --border:#e3e8f0; --border-2:#d3dae6; --fg:#1b2435; --muted:#5b6880; --faint:#8793a6; --src-text:#3d4a60;
  --accent:#4f46e5;
  --paid:#15774a; --paid-bg:#dcf3e6; --review:#a15c0b; --review-bg:#fcecd0; --rej:#b4241f; --rej-bg:#fbe0de;
  --warn:#8a6512; --warn-bg:#f7eecb; --esc-bar:#e0a24a; --fraud-bd:#f0b8b4; --fraud-tx:#7d1a16;
  --bulk-rej:#f1867d; --bulk-muted:#9aa6bb;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:"Inter",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --bg:#0c1018; --surface:#141b27; --surface-2:#10151f; --sel:#1b2440;
  --border:#232c3b; --border-2:#2f3a4d; --fg:#e4e9f2; --muted:#9aa6bb; --faint:#687689; --src-text:#aab6cc;
  --accent:#7c84f5;
  --paid:#4fcd8e; --paid-bg:#113526; --review:#e0a24a; --review-bg:#372712; --rej:#f1867d; --rej-bg:#381815;
  --warn:#dcc474; --warn-bg:#312813; --esc-bar:#e0a24a; --fraud-bd:#5a2420; --fraud-tx:#f1b3ad;
  color-scheme:dark;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
.page{width:1440px;max-width:100%;margin:0 auto}
.mono{font-family:var(--mono)}
.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.cur{font-size:.82em;color:var(--faint)}

/* header */
.header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 24px;background:var(--surface);border-bottom:1px solid var(--border)}
.h-left{display:flex;align-items:center;gap:28px}
.brand .eyebrow{font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
.brand h1{margin:0;font-size:19px;font-weight:700;letter-spacing:-.01em}
.tabs{display:flex;gap:4px;padding:3px;background:var(--surface-2);border:1px solid var(--border);border-radius:9px}
.tab{padding:6px 12px;border:0;border-radius:7px;background:transparent;font:inherit;font-size:13px;font-weight:500;color:var(--muted);cursor:pointer}
.tab.active{background:var(--surface);font-weight:600;color:var(--fg);box-shadow:0 1px 2px rgba(0,0,0,.06)}
.tab[disabled]{opacity:.55;cursor:not-allowed}
.tab-n{font-family:var(--mono);color:var(--review)}
.tab.active .tab-n{color:var(--review)}
.tab[disabled] .tab-n{color:var(--faint)}
.h-right{font-family:var(--mono);font-size:12px;color:var(--muted)}

/* summary strip */
.strip{display:grid;grid-template-columns:1.5fr 1fr 1fr 1fr 1fr;gap:12px;padding:16px 24px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 16px}
.tile-k{font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.tile-v{font-family:var(--mono);font-size:20px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.tile-v.review{color:var(--review)}.tile-v.rej{color:var(--rej)}.tile-v.paid{color:var(--paid)}
.tile-of{font-size:13px;color:var(--faint);font-weight:400}
.tile-sub{font-size:12px;color:var(--muted);margin-top:3px}
.accent-l{border-left-width:3px}.accent-l.review{border-left-color:var(--review)}.accent-l.rej{border-left-color:var(--rej)}
.tile-top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.tile-billed{font-family:var(--mono);font-size:12px;color:var(--muted)}
.bar{display:flex;gap:2px;height:10px;border-radius:5px;overflow:hidden;margin:9px 0 8px}
.bar .seg{display:block}.seg.paid{background:var(--paid)}.seg.rej{background:var(--rej)}.seg.esc{background:var(--esc-bar)}
.bar-legend{display:flex;gap:16px;font-size:12px;color:var(--muted)}
.bar-legend .d{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;vertical-align:1px}
.d.paid{background:var(--paid)}.d.rej{background:var(--rej)}.d.esc{background:var(--esc-bar)}

/* workspace */
.workspace{display:grid;grid-template-columns:370px minmax(0,1fr) 400px;gap:12px;padding:0 24px 24px;height:820px}
.pane{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}

/* queue */
.q-controls{padding:12px 14px;border-bottom:1px solid var(--border)}
.seg-ctl{display:flex;padding:3px;background:var(--surface-2);border:1px solid var(--border);border-radius:8px}
.seg-btn{flex:1;padding:5px 8px;border:0;border-radius:6px;background:transparent;font:inherit;font-size:12.5px;font-weight:600;color:var(--muted);cursor:pointer}
.seg-btn.active{background:var(--surface);color:var(--fg);box-shadow:0 1px 2px rgba(0,0,0,.08)}
.seg-n{font-family:var(--mono);opacity:.85}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border:1px solid var(--border-2);border-radius:999px;background:var(--surface);font:inherit;font-size:11.5px;font-weight:500;color:var(--muted);cursor:pointer}
.chip.fraud-chip{border-color:var(--fraud-bd);color:var(--rej)}
.chip.active{background:var(--fg);border-color:var(--fg);color:var(--surface)}
.chip.fraud-chip.active{background:var(--rej);border-color:var(--rej);color:#fff}
.bulk{display:flex;align-items:center;gap:8px;padding:9px 14px;background:var(--fg);color:var(--surface);font-size:12.5px}
.bulk-n{font-weight:600;flex:1}
.bulk-btn{border:0;border-radius:6px;padding:4px 10px;font:inherit;font-size:12px;font-weight:600;cursor:pointer}
.bulk-btn.route{background:transparent;color:#fff;border:1px solid rgba(255,255,255,.3)}
.bulk-btn.reject{background:var(--bulk-rej);color:var(--fg)}
.bulk-btn.clear{background:transparent;color:var(--bulk-muted)}
.q-scroll{overflow:auto;flex:1}
.qgroup[hidden]{display:none}
.qg-head{position:sticky;top:0;display:flex;justify-content:space-between;align-items:center;padding:8px 14px;background:var(--surface-2);border-top:1px solid var(--border);border-bottom:1px solid var(--border);z-index:1}
.qgroup:first-child .qg-head{border-top:0}
.qg-t{display:flex;align-items:center;gap:7px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.qg-t .gdot{width:7px;height:7px;border-radius:50%;background:currentColor}
.qg-t.rej{color:var(--rej)}.qg-t.review{color:var(--review)}.qg-t.muted{color:var(--muted)}
.qg-n{font-family:var(--mono);font-size:11px;color:var(--faint)}
.qrow{display:grid;grid-template-columns:18px minmax(0,1fr) auto;gap:10px;width:100%;padding:11px 14px;border:0;border-bottom:1px solid var(--border);background:transparent;text-align:left;cursor:pointer;font:inherit;color:inherit}
.qrow[hidden]{display:none}
.qrow:hover{background:var(--surface-2)}
.qrow[aria-current="true"]{background:var(--sel);box-shadow:inset 3px 0 0 var(--accent)}
.qcheck{width:16px;height:16px;margin-top:1px;border:1.5px solid var(--border-2);border-radius:4px;cursor:pointer}
.qcheck[aria-checked="true"]{background:var(--accent);border-color:var(--accent);position:relative}
.qcheck[aria-checked="true"]::after{content:"";position:absolute;left:4.5px;top:1px;width:4px;height:8px;border:solid #fff;border-width:0 2px 2px 0;transform:rotate(45deg)}
.qmain{min-width:0;display:flex;flex-direction:column;gap:3px}
.qline1{display:flex;align-items:center;gap:6px;min-width:0}
.qvendor{font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.qtag{font-size:9.5px;font-weight:700;padding:1px 6px;border-radius:4px}
.qtag:empty{display:none}
.qtag.paid{background:var(--paid-bg);color:var(--paid)}.qtag.rej{background:var(--rej-bg);color:var(--rej)}.qtag.routed{background:var(--sel);color:var(--accent)}
.qrow.decided .qvendor{color:var(--muted)}
.qline2{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.qnum{font-family:var(--mono);font-size:11px;color:var(--faint)}
.qline3{display:flex;gap:6px;align-items:center}
.due{font-size:10.5px;font-weight:600;padding:1px 7px;border-radius:4px}
.due.rej{background:var(--rej-bg);color:var(--rej)}.due.review{background:var(--review-bg);color:var(--review)}.due.muted{background:var(--surface-2);color:var(--muted)}
.owner{font-size:10.5px;padding:1px 7px;border-radius:4px;background:var(--surface-2);border:1px solid var(--border);color:var(--muted)}
.qright{text-align:right}
.qamt{display:block;font-family:var(--mono);font-size:12.5px;font-variant-numeric:tabular-nums}
.qcur{font-size:10.5px;color:var(--faint)}

/* detail */
.detail{position:relative}
.dv{display:none;flex-direction:column;min-height:0;flex:1;overflow:hidden}
.dv.on{display:flex}
.dv-empty{display:flex;flex:1;align-items:center;justify-content:center;color:var(--faint)}
.detail:has(.dv.on) .dv-empty{display:none}
.d-head{display:flex;justify-content:space-between;gap:16px;padding:18px 22px 14px;border-bottom:1px solid var(--border)}
.d-head-l{min-width:0}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;margin-bottom:8px}
.pill .dot{width:8px;height:8px;border-radius:50%}
.pill.review{color:var(--review)}.pill.review .dot{background:var(--review)}
.pill.paid{color:var(--paid)}.pill.paid .dot{background:var(--paid)}
.pill.rej{color:var(--rej)}.pill.rej .dot{background:var(--rej)}
.pill.accent{color:var(--accent)}.pill.accent .dot{background:var(--accent)}
.d-vendor{font-size:21px;font-weight:700;letter-spacing:-.01em;line-height:1.2}
.d-meta{font-family:var(--mono);font-size:12px;color:var(--muted);margin-top:4px}
.d-head-r{text-align:right;white-space:nowrap}
.d-amt{font-family:var(--mono);font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.d-head-r .due{display:inline-block;margin-top:6px;font-size:11.5px;padding:2px 8px}
.d-body{flex:1;overflow:auto;padding:16px 22px;display:flex;flex-direction:column;gap:18px}
.sec-t{margin:0;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.sec-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:8px}
.sec-note{font-size:11.5px;color:var(--faint)}
.fraud{display:flex;gap:12px;align-items:flex-start;background:var(--rej-bg);border:1px solid var(--fraud-bd);border-radius:9px;padding:12px 14px;color:var(--fraud-tx);font-size:13px}
.fraud-mark{flex:none;width:22px;height:22px;border-radius:50%;background:var(--rej);color:#fff;font-weight:700;font-size:13px;display:flex;align-items:center;justify-content:center}
.pstrip{display:grid;grid-template-columns:repeat(5,1fr);border:1px solid var(--border);border-radius:9px;overflow:hidden}
.ps-cell{display:flex;gap:8px;padding:9px 12px;border-right:1px solid var(--border)}
.ps-cell:last-child{border-right:0}
.ps-dot{width:9px;height:9px;border-radius:50%;margin-top:3px;flex:none}
.ps-dot.accent{background:var(--accent)}.ps-dot.review{background:var(--review)}.ps-dot.warn{background:var(--warn)}.ps-dot.muted{background:var(--border-2)}
.ps-lab{font-size:12px;font-weight:600}
.ps-det{font-size:11.5px;color:var(--muted)}
.d-sec{display:flex;flex-direction:column}
.evidence{background:var(--surface-2);border:1px solid var(--border);border-radius:9px;padding:13px 15px}
.why{margin:0 0 10px;font-size:14px;line-height:1.55;max-width:68ch;text-wrap:pretty}
.flags{display:flex;flex-wrap:wrap;gap:6px}
.flag{font-family:var(--mono);font-size:11px;padding:3px 9px;border-radius:6px}
.flag.critical{background:var(--rej-bg);color:var(--rej)}.flag.review{background:var(--review-bg);color:var(--review)}.flag.warning{background:var(--warn-bg);color:var(--warn)}
.flag b{font-weight:400;opacity:.72}
.lt{border:1px solid var(--border);border-radius:8px;overflow:hidden;font-size:13px}
.lt-row{display:grid;grid-template-columns:minmax(0,1fr) 70px 100px 110px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--border)}
.lt-row:last-child{border-bottom:0}
.lt-head{background:var(--surface-2);font-size:10.5px;font-weight:600;text-transform:uppercase;color:var(--muted)}
.lt-item{min-width:0}
.lt-note{display:block;font-family:var(--mono);font-size:11px;margin-top:2px}
.lt-row.flag-review{background:var(--review-bg);box-shadow:inset 3px 0 0 var(--review)}
.lt-row.flag-review .lt-note{color:var(--review)}
.lt-row.flag-warning{background:var(--warn-bg);box-shadow:inset 3px 0 0 var(--warn)}
.lt-row.flag-warning .lt-note{color:var(--warn)}
.lt-foot,.lt-total{grid-template-columns:minmax(0,1fr) 110px;color:var(--muted);font-size:12px}
.lt-flabel{text-align:right}
.lt-total{color:var(--fg);font-weight:600;border-top:1.5px solid var(--border-2)}
.lt-total.flag-warning{background:var(--warn-bg)}
.lt-tnote{display:block;font-family:var(--mono);font-size:11px;color:var(--warn);font-weight:400}
.lt-foot .num,.lt-total .num{min-width:86px}

/* decision */
.decision{border-top:1px solid var(--border);padding:14px 22px}
.dec-labs{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.dec-hint{font-size:11.5px;color:var(--faint)}
.revnote{font-size:12.5px;color:var(--accent);background:var(--sel);border:1px solid var(--border);border-radius:7px;padding:8px 11px}
.presets{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.preset{border:1px dashed var(--border-2);background:var(--surface-2);color:var(--muted);border-radius:6px;padding:3px 9px;font:inherit;font-size:11.5px;cursor:pointer}
.dec-row{display:flex;gap:8px}
.reason{flex:1;min-width:0;height:40px;border:1px solid var(--border-2);border-radius:8px;padding:0 13px;background:var(--surface);color:var(--fg);font:inherit;font-size:13.5px}
.reason.invalid{border-color:var(--rej)}
.btn{height:40px;border-radius:8px;border:1px solid var(--border-2);background:var(--surface);color:var(--fg);font:inherit;font-size:13px;font-weight:600;padding:0 16px;cursor:pointer;white-space:nowrap}
.btn.reject{color:var(--rej);border-color:var(--rej)}
.btn.approve{background:var(--accent);border-color:var(--accent);color:#fff}
.dec-done{display:none;align-items:center;gap:9px;font-size:13.5px}
.dec-done .dot{width:8px;height:8px;border-radius:50%}
.dec-done .undo{margin-left:auto;border:0;background:transparent;color:var(--accent);font:inherit;font-size:12.5px;font-weight:600;cursor:pointer}
.dv.decided .dec-open{display:none}
.dv.decided .dec-done{display:flex}

/* source */
.source{min-width:0}
.sv{display:none;flex-direction:column;min-height:0;flex:1;overflow:hidden}
.sv.on{display:flex}
.sv-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border)}
.sv-file{font-family:var(--mono);font-size:12px;color:var(--fg);margin-top:3px}
.badge{font-size:10.5px;font-weight:600;color:var(--accent);background:var(--sel);padding:2px 8px;border-radius:4px}
.sv-body{flex:1;overflow:auto;background:var(--surface-2);padding:10px 0;font-family:var(--mono);font-size:11.5px;line-height:1.75}
.src-row{display:grid;grid-template-columns:34px minmax(0,1fr)}
.src-n{text-align:right;color:var(--faint);user-select:none;padding-right:10px}
.src-t{white-space:pre-wrap;color:var(--src-text);padding-right:12px}
.src-row.flag-review{background:var(--review-bg);box-shadow:inset 3px 0 0 var(--review)}
.src-row.flag-warning{background:var(--warn-bg);box-shadow:inset 3px 0 0 var(--warn)}
.src-row.flag-review .src-t,.src-row.flag-warning .src-t{color:var(--fg)}
.sv-legend{display:flex;align-items:center;gap:8px;padding:10px 16px;border-top:1px solid var(--border);font-size:11.5px;color:var(--muted)}
.sw{width:12px;height:12px;border-radius:3px;display:inline-block}
.sw.review{background:var(--review-bg);box-shadow:inset 2px 0 0 var(--review)}
.sw.warning{background:var(--warn-bg);box-shadow:inset 2px 0 0 var(--warn);margin-left:8px}

.preview-note{margin:0;padding:0 24px 24px;font-size:12px;color:var(--faint)}
.toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%) translateY(16px);background:var(--fg);color:var(--bg);padding:11px 18px;border-radius:9px;font-size:13px;font-weight:500;box-shadow:0 8px 24px rgba(0,0,0,.25);opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;z-index:30}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
"""

_JS = r"""
(function(){
  var vm = JSON.parse(document.getElementById('vm').textContent);
  var decisions = {};
  // In live mode the ledger is the source of truth; ignore any stale simulated state.
  if(!vm.live){ try { decisions = JSON.parse(localStorage.getItem('ap_wb3')||'{}'); } catch(e){} }
  var ownerFilter='all', flagFilter='all', checked=new Set();
  function isResolved(k){ var d=decisions[k]; return !!d && (d.kind==='paid' || d.kind==='rejected'); }
  var money=function(n){return n.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});};
  var rows=[].slice.call(document.querySelectorAll('.qrow'));
  var byId=function(id){return document.getElementById(id);};

  function select(key){
    document.querySelectorAll('.dv').forEach(function(d){d.classList.toggle('on',d.dataset.key===key);});
    document.querySelectorAll('.sv').forEach(function(s){s.classList.toggle('on',s.dataset.key===key);});
    rows.forEach(function(r){r.setAttribute('aria-current', String(r.dataset.key===key));});
  }
  function applyFilter(){
    document.querySelectorAll('.qgroup').forEach(function(g){
      var shown=0;
      g.querySelectorAll('.qrow').forEach(function(r){
        var ok=(ownerFilter==='all'||r.dataset.owner===ownerFilter) &&
               (flagFilter==='all'||(' '+r.dataset.cats+' ').indexOf(' '+flagFilter+' ')>-1);
        r.hidden=!ok; if(ok) shown++;
      });
      g.hidden=shown===0;
    });
  }
  function recount(){
    var open=0, held=0;
    // Routing does not resolve an invoice, so a routed row still counts as open and held.
    vm.keys.forEach(function(k){ if(!isResolved(k)){ open++; held+=vm.amounts[k]; } });
    byId('open-count').textContent=open; byId('tab-open').textContent=open;
    byId('held-amt').textContent='$'+money(held);
  }
  function toast(msg){var t=byId('toast');t.textContent=msg;t.classList.add('show');clearTimeout(t._t);t._t=setTimeout(function(){t.classList.remove('show');},2600);}

  function markDecided(key,kind,reason){
    var resolved = (kind==='paid'||kind==='rejected');
    var row=document.querySelector('.qrow[data-key="'+CSS.escape(key)+'"]');
    if(row){ row.classList.toggle('decided', resolved);
      var tag=row.querySelector('[data-tag]');
      var label=kind==='paid'?'PAID':kind==='rejected'?'REJECTED':'ROUTED';
      tag.textContent=label; tag.className='qtag '+(kind==='paid'?'paid':kind==='rejected'?'rej':'routed');
    }
    var dv=document.querySelector('.dv[data-key="'+CSS.escape(key)+'"]');
    if(dv){
      var tone=kind==='paid'?'paid':kind==='rejected'?'rej':'accent';
      var text=kind==='paid'?'Approved and paid':kind==='rejected'?'Rejected by reviewer':'Routed to Controller';
      var pill=dv.querySelector('[data-pill]'); pill.className='pill '+tone;
      dv.querySelector('[data-pill-text]').textContent=text;
      // Routing leaves the invoice open, so the decision footer stays available.
      dv.classList.toggle('decided', resolved);
      if(resolved){
        dv.querySelector('.dec-done .dot').style.background='var(--'+tone+')';
        dv.querySelector('.dec-done-text').textContent=text+(reason?' — “'+reason+'”':'')+' · AP clerk · just now';
      }
    }
  }
  function commit(key,kind,reason){
    decisions[key]={kind:kind,reason:reason};
    if(!vm.live){ try{ localStorage.setItem('ap_wb3',JSON.stringify(decisions)); }catch(e){} }
    markDecided(key,kind,reason); recount();
    var dv=document.querySelector('.dv[data-key="'+CSS.escape(key)+'"]');
    var who=dv?dv.querySelector('.d-vendor').textContent:key;
    toast(who+' — '+(kind==='paid'?'approved and paid':kind==='rejected'?'rejected':'routed to Controller'));
    if(kind==='paid'||kind==='rejected'){ var next=vm.keys.filter(function(k){return !isResolved(k);})[0]; if(next) select(next); }
  }
  function decide(key,kind,reason,number){
    if(vm.live){
      var map={paid:'approve',rejected:'reject',routed:'route'};
      fetch('/api/decision',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({invoice_number:number,decision:map[kind],reason:reason})})
        .then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
        .then(function(res){ if(!res.ok||!res.d||!res.d.ok){ toast((res.d&&res.d.error)||'Decision failed, nothing was saved.'); return; }
          commit(key,kind,reason); })
        .catch(function(){ toast('Network error, nothing was saved.'); });
    } else { commit(key,kind,reason); }
  }

  rows.forEach(function(r){
    r.addEventListener('click',function(){ select(r.dataset.key); });
    var cb=r.querySelector('[data-check]');
    function toggle(e){ e.stopPropagation();
      var on=cb.getAttribute('aria-checked')!=='true'; cb.setAttribute('aria-checked',String(on));
      if(on) checked.add(r.dataset.key); else checked.delete(r.dataset.key); renderBulk();
    }
    cb.addEventListener('click',toggle);
    cb.addEventListener('keydown',function(e){ if(e.key===' '||e.key==='Enter'){e.preventDefault();toggle(e);} });
  });

  document.querySelectorAll('.seg-btn').forEach(function(b){
    b.addEventListener('click',function(){
      document.querySelectorAll('.seg-btn').forEach(function(x){x.classList.remove('active');});
      b.classList.add('active'); ownerFilter=b.dataset.owner; applyFilter();
    });
  });
  document.querySelectorAll('.chip').forEach(function(c){
    c.addEventListener('click',function(){
      if(c.classList.contains('active')&&c.dataset.cat!=='all'){ flagFilter='all';
        document.querySelectorAll('.chip').forEach(function(x){x.classList.toggle('active',x.dataset.cat==='all');});
      } else { flagFilter=c.dataset.cat;
        document.querySelectorAll('.chip').forEach(function(x){x.classList.toggle('active',x===c);});
      }
      applyFilter();
    });
  });

  var bulk=document.querySelector('[data-bulk]');
  function renderBulk(){
    if(!bulk) return;
    bulk.hidden=checked.size===0;
    var n=bulk.querySelector('[data-bulk-n]'); if(n) n.textContent=checked.size+' selected';
  }
  document.querySelectorAll('[data-bulk-act]').forEach(function(b){
    b.addEventListener('click',function(){
      var act=b.dataset.bulkAct;
      if(act==='clear'){ checked.forEach(function(k){var c=document.querySelector('.qrow[data-key="'+CSS.escape(k)+'"] [data-check]'); if(c)c.setAttribute('aria-checked','false');}); checked.clear(); renderBulk(); return; }
      var reason=act==='route'?'Routed in bulk':'Rejected in bulk';
      Array.from(checked).forEach(function(k){ decide(k, act==='route'?'routed':'rejected', reason, vm.numbers[k]); });
      checked.clear(); renderBulk();
    });
  });

  document.querySelectorAll('.dv').forEach(function(dv){
    var key=dv.dataset.key;
    if(decisions[key]) markDecided(key,decisions[key].kind,decisions[key].reason);
    dv.querySelectorAll('.preset').forEach(function(p){
      p.addEventListener('click',function(){ dv.querySelector('.reason').value=p.textContent; });
    });
    dv.querySelectorAll('[data-act]').forEach(function(btn){
      btn.addEventListener('click',function(){
        var act=btn.dataset.act, input=dv.querySelector('.reason'), reason=input.value.trim();
        if((act==='reject'||act==='route')&&!reason){ input.classList.add('invalid'); input.focus(); return; }
        input.classList.remove('invalid');
        var kind=act==='approve'?'paid':act==='reject'?'rejected':'routed';
        if(!reason) reason=act==='approve'?'Approved by reviewer':reason;
        decide(key,kind,reason,dv.dataset.number);
      });
    });
    var undo=dv.querySelector('.undo');
    // A payment cannot be undone, so Undo is only offered for the simulated static preview.
    if(undo && vm.live){ undo.style.display='none'; }
    else if(undo) undo.addEventListener('click',function(){
      delete decisions[key]; try{localStorage.setItem('ap_wb3',JSON.stringify(decisions));}catch(e){}
      dv.classList.remove('decided');
      var row=document.querySelector('.qrow[data-key="'+CSS.escape(key)+'"]');
      if(row){ row.classList.remove('decided'); var tag=row.querySelector('[data-tag]'); tag.textContent=''; tag.className='qtag'; }
      var pill=dv.querySelector('[data-pill]'); pill.className='pill review';
      dv.querySelector('[data-pill-text]').textContent='Needs your review';
      recount();
    });
  });

  applyFilter(); recount();
  var firstOpen=vm.keys.filter(function(k){return !isResolved(k);})[0]||vm.keys[0];
  if(firstOpen) select(firstOpen);
})();
"""
