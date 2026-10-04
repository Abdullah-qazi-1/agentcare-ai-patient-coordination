"""Wires the `evals/` datasets into the regular test run as release gates.

`evals/*.py` are also directly runnable (`uv run python -m evals.safety_redteam`) per
the architecture blueprint's intent, but a gate that only runs when someone remembers
to run it separately is not a gate — this makes `uv run pytest` fail the build the same
way a broken unit test would if either dataset regresses.
"""

from evals import routing_accuracy, safety_redteam


class TestSafetyRedTeamGate:
    def test_all_cases_pass(self):
        report = safety_redteam.run()
        assert not report.failures, "\n".join(report.failures)


class TestRoutingAccuracyGate:
    def test_all_cases_pass(self):
        report = routing_accuracy.run()
        assert not report.failures, "\n".join(report.failures)
