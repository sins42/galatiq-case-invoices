# Design decisions

This document explains why the system is built the way it is, what was traded off, and what it does not handle yet. For how it works, see [architecture.md](architecture.md).

## Scope

Built: the full pipeline end to end on all five formats (TXT, JSON, CSV, XML, and PDF), with the mock provider and the Grok adapter, the full set of validation checks, the ledger, batch processing with run metrics and cost tracking, the human review flow (approve, reject, route) over both the CLI and a live localhost workbench, a self-contained HTML review workbench, audit records, and tests.

Added next: recorded model responses, to replay real model output offline.

Out of scope: credit memos, and real purchase order and goods receipt feeds.

## Key decisions

**1. LangGraph for orchestration.** The workflow is a state machine with fixed stages, two loops, and branches, which is exactly what LangGraph models. CrewAI and AutoGen are built for open-ended agent conversation, which is the opposite of what a payment workflow needs. A hand-written orchestrator would also work, but it would rebuild what LangGraph already provides.

**2. Code decides, the model advises.** Every check that can move money is deterministic and testable. The model is limited to reading unstructured text and weighing borderline cases. A clean invoice under the threshold never calls the model at all, which keeps the common case fast and cheap.

**3. Parse structured formats, use the model for text.** JSON, XML, and the known CSV layouts are parsed exactly, with no model call and no hallucination risk. TXT and PDF go to the model. Any structured file that does not fit falls back to the model. Sending everything through the model would be simpler, but less accurate and more expensive.

**4. Self-correction loops are grounded in real checks.** The extraction loop retries on a concrete error: a Pydantic schema failure, or an extracted value that does not appear in the source text. The approval critic must cite a specific flag or fact to object. A model grading its own work with no outside reference is unreliable, so neither loop relies on that. Both loops have hard caps and escalate when the cap is reached.

**5. The approval agent can approve or escalate, never reject.** A rejection is a final "no." It is only issued by deterministic rules, in cases where no human could make the invoice payable. For a judgment call, the safe alternative to approving is asking a person.

**6. Reject versus escalate.** Neither outcome pays a bad invoice, so the deciding test is whether the invoice could be legitimate with human context. A missing vendor, an exact duplicate, or a negative quantity has no legitimate reading, so it rejects. An unknown item could be a new product, and an overstock could be a large legitimate order, so these escalate.

**7. Inventory stands in for three-way matching.** Real accounts payable checks an invoice against the purchase order and the goods receipt. There is no feed for either here, so stock levels act as the proxy. The production path is to match against purchase order quantities.

**8. Duplicates are found by content, not by the revision field.** The duplicate check hashes the normalized invoice content: number, vendor, currency, total, and line items. The same invoice in two formats produces the same hash, and an amendment is caught even if the vendor never marks it as revised.

| Situation | Outcome |
|---|---|
| Same number, vendor, and content | Reject as a duplicate |
| Same number and vendor, different content | Escalate as a revision, showing the dollar difference |
| Same content under a new number | Escalate, since it could be a rebill or a genuine repeat order |
| Number one character away from a processed one, same vendor and total | Escalate as a possible typo duplicate |
| Same number, different vendor | Escalate as a collision |

Near-matching numbers are only flagged when the vendor and total also match, because vendors number invoices sequentially and INV-1001 followed by INV-1002 is normal. A genuine repeat order has a new invoice number, so it is never auto-rejected. It escalates with the earlier invoice and the time gap attached, so a person can tell a repeat from a rebill.

**9. Fuzzy matching only where it raises scrutiny.** Item names must match the catalog exactly after normalizing case and spacing. "WidgetC" is one letter away from "WidgetA", and a fuzzy match there would let an unknown item pass. A trailing qualifier such as "(rush order)" is stripped, but only an exact catalog match counts. Fuzzy matching is used for invoice numbers, where a near match triggers a review rather than a pass.

**10. Tolerances are asymmetric.** Matching totals to the penny would flag ordinary rounding. Overcharges are only allowed at rounding scale, the larger of 0.1 percent or 1 dollar, so even small unexplained extras are flagged. INV-1013 has a 50 dollar gap that a 1 percent tolerance would have let through. Undercharges get a looser band, since being billed less is not a loss.

**11. The system runs with no API key.** A provider interface selects Grok when an API key is set and a deterministic mock otherwise, so anyone can run the system offline and tests are fully reproducible. Recorded real responses are a planned third backend between the two.

**12. A full audit log.** Each stage records its inputs, outputs, and any prompt and model response, so every decision can be reconstructed. The cost is size and sensitivity. Redaction and retention are left for later.

**13. The critic shares the approver's model.** The critic reviews the approver's draft on the same model that produced it. A separate or stronger critic model would be more independent and less likely to agree with reasoning it would have written itself, but there is no evidence yet that the shared-model critic rubber-stamps. The provider already keys models by role, so adding a separate critic slot is a small change. It is deferred until the eval harness exists and can measure whether a distinct critic model catches more bad approvals.

## Expected outcomes for the sample invoices

| Invoice | Format | What it tests | Expected |
|---|---|---|---|
| 1001 | txt | Clean | Pay |
| 1002 | txt | Overstock, misspelled headers | Escalate |
| 1003 | txt | Zero-stock item, pressure note, 100,000 total | Escalate |
| 1004 | json | Clean | Pay |
| 1004 revised | json | Revision of 1004 | Escalate (after 1004) |
| 1005 | json | Overstock | Escalate |
| 1006 | csv | Clean, key-value layout | Pay |
| 1007 | csv | Overstock | Escalate |
| 1008 | txt | Unknown items inside an email | Escalate |
| 1009 | json | Negative quantity and total, no vendor | Reject |
| 1010 | txt | Repeated item, rush-order line at 300 against 250, shipping | Price Warning, so the approval agent reviews it instead of auto-paying |
| 1011 | txt and pdf | Same invoice in two formats | First pays, second rejects as a duplicate |
| 1012 | txt and pdf | OCR errors, total of 9,975 | First pays, second rejects as a duplicate |
| 1013 | json and pdf | Overstock only once lines are summed, 50 dollar gap | Escalate |
| 1014 | xml | EUR invoice | Escalate |
| 1015 | csv | Clean, row-per-item layout | Pay |
| 1016 | json | Unknown item WidgetC | Escalate |

The automated tests in `tests/` run every invoice above end to end and assert these outcomes across all five formats.

Extra test cases cover: quantity exactly at stock, a total exactly at the threshold, a rebill under a new number, a mistyped invoice number, a number collision, an injection note, a failed payment, and re-running a paid invoice.

## Known limitations and open questions

- **Stock is not consumed.** Paying an invoice does not reduce stock, so two invoices could each pass against the same goods. Matching against purchase order quantities fixes this.
- **Split billing.** Handled by a check that totals a vendor's non-rejected invoices over a recent window and raises a Review flag when the running sum crosses the scrutiny threshold. The window is based on processing time, so a production system would also key it on invoice dates.
- **Credit memos.** Negative amounts are treated as errors, which is correct for this data but not for real credit notes.
- **Audit log contents.** It stores full invoice text and prompts, so it needs redaction and retention rules before real use.
- **SQLite.** SQLite allows only one writer at a time, which limits parallel processing. The production path is a server database such as Postgres, with no change to the data model.
