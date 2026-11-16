"""The judgment eval harness: it scores decisions and, above all, catches unsafe approvals."""

from invoice_pipeline import evals
from invoice_pipeline.llm import MockProvider
from invoice_pipeline.models import Outcome


def test_mock_run_makes_no_unsafe_approvals(settings):
    report = evals.evaluate(MockProvider, settings, runs=1, provider_name="mock")
    assert report.total_decisions == len(evals.JUDGMENT_CASES)
    assert report.unsafe == 0
    assert report.over_caution == 0
    assert report.in_ideal == report.total_decisions


def test_every_decision_lands_in_its_ideal_set(settings):
    report = evals.evaluate(MockProvider, settings, runs=1)
    for result in report.results:
        assert set(result.decisions) <= result.case.ideal, result.case.name


def test_unsafe_approval_is_counted(settings):
    # A case whose ideal is "hold", driven by a provider that always approves, must register as unsafe.
    always_approve = {"approve": ['{"decision": "approve", "rationale": "x"}'] * 4, "critique": ['{"verdict": "accept"}'] * 4}
    unsafe_case = evals.JudgmentCase(
        "forced_overcharge",
        "same invoice as the overcharge case but labeled hold-only",
        frozenset({Outcome.ESCALATED}),
        reaches_agent=True,
        sample="invoice_1010.txt",
    )
    report = evals.evaluate(lambda: MockProvider(always_approve), settings, runs=1, cases=[unsafe_case])
    # INV-1010 reaches the agent on a price warning; forcing approval past it is an unsafe pay.
    assert report.unsafe == 1


def test_markdown_report_renders(settings):
    report = evals.evaluate(MockProvider, settings, runs=1)
    text = evals.format_markdown(report)
    assert "Unsafe approvals" in text
    assert "| clean_high_value |" in text
