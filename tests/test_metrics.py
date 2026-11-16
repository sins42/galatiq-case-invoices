"""Run metrics and cost estimation over a batch of invoices."""

import dataclasses

from conftest import SAMPLES

from invoice_pipeline import metrics
from invoice_pipeline.graph import InvoicePipeline
from invoice_pipeline.llm import MockProvider


def test_batch_metrics_and_cost(settings, conn):
    provider = MockProvider()
    pipeline = InvoicePipeline(settings, provider, conn)
    samples = ["invoice_1001.txt", "invoice_1003.txt", "invoice_1009.json"]
    states = [pipeline.run(SAMPLES / name, run_id=f"t{i}") for i, name in enumerate(samples)]

    m = metrics.summarize(states, provider.usage, settings)
    assert (m.total, m.paid, m.escalated, m.rejected) == (3, 1, 1, 1)
    assert m.dollars_paid > 0 and m.dollars_held > 0
    assert m.model_calls == len(provider.usage) >= 1
    assert m.cost is None  # prices not configured

    priced = dataclasses.replace(settings, price_in_per_mtok=1.0, price_out_per_mtok=1.0)
    assert metrics.estimated_cost(provider.usage, priced) == 0.0  # the mock reports zero tokens


def test_exact_duplicate_is_not_double_counted_in_dollars(settings, conn):
    provider = MockProvider()
    pipeline = InvoicePipeline(settings, provider, conn)
    first = pipeline.run(SAMPLES / "invoice_1011.pdf", run_id="a")  # paid, 3000
    second = pipeline.run(SAMPLES / "invoice_1011.txt", run_id="b")  # rejected duplicate

    m = metrics.summarize([first, second], provider.usage, settings)
    assert m.dollars_paid == 3000.0
    assert m.dollars_held == 0.0  # the duplicate is not counted again
    assert m.rejected == 1  # but it is still a processed file
