"""The rule engine is the part an auditor will read. Test it like it matters."""

from __future__ import annotations

import pytest

from agentflow.config import rules_config
from agentflow.rules import RuleError, apply_rules, evaluate, highest_priority


class TestExpressionSafety:
    """The expressions come from a YAML file that policy owners edit."""

    @pytest.mark.parametrize(
        "expr",
        [
            "__import__('os').system('rm -rf /')",
            "().__class__.__bases__[0].__subclasses__()",
            "open('/etc/passwd').read()",
            "[x for x in range(10)]",
            "exec('import os')",
            "severity.__class__",
            "lambda: 1",
        ],
    )
    def test_dangerous_expressions_are_rejected(self, expr: str) -> None:
        with pytest.raises(RuleError):
            evaluate(expr, {"severity": "high"})

    def test_missing_variable_is_falsy_not_an_error(self) -> None:
        assert evaluate("nonexistent > 5", {}) is False

    def test_comparing_none_to_a_number_does_not_raise(self) -> None:
        # Happens whenever an upstream agent degrades and leaves a field unset.
        assert evaluate("confidence < 0.5", {"confidence": None}) is False

    def test_division_by_zero_is_contained(self) -> None:
        assert evaluate("a / b > 1", {"a": 1, "b": 0}) is False


class TestLiteralAliases:
    """Policy authors are not Python programmers."""

    @pytest.mark.parametrize("literal", ["true", "True"])
    def test_true_spellings(self, literal: str) -> None:
        assert evaluate(f"safety_risk == {literal}", {"safety_risk": True}) is True

    @pytest.mark.parametrize("literal", ["false", "False"])
    def test_false_spellings(self, literal: str) -> None:
        assert evaluate(f"safety_risk == {literal}", {"safety_risk": False}) is True


class TestConfiguredRules:
    def test_every_rule_compiles(self) -> None:
        for rule in rules_config()["rules"]:
            evaluate(rule["when"], {})

    def test_every_rule_carries_a_citation(self) -> None:
        """No uncited verdicts. This is the whole trust model."""
        uncited = [r["id"] for r in rules_config()["rules"] if not r.get("citation")]
        assert uncited == []

    def test_every_rule_declares_actions_and_priority(self) -> None:
        for rule in rules_config()["rules"]:
            assert rule.get("priority"), f"{rule['id']} has no priority"
            assert rule.get("actions"), f"{rule['id']} has no actions"

    def test_injury_always_fires_the_hse_rule(self) -> None:
        findings, errored = apply_rules(
            rules_config()["rules"],
            {"injury_reported": True, "near_miss": False, "asset_resolved": True,
             "severity": "low", "asset_criticality": 1, "confidence": 0.9, "citation_count": 2},
            "conveyor",
        )
        assert errored == []
        assert "SAF-001" in {f.rule_id for f in findings}
        assert highest_priority(findings) == "P1"

    def test_unresolved_asset_blocks_automatic_action(self) -> None:
        findings, _ = apply_rules(
            rules_config()["rules"],
            {"asset_resolved": False, "severity": "critical", "asset_criticality": 5,
             "confidence": 0.9, "citation_count": 3},
            None,
        )
        ids = {f.rule_id for f in findings}
        assert "AIQ-002" in ids
        # An unidentified asset must not generate a work order.
        assert "OPS-001" not in ids

    def test_findings_are_sorted_most_urgent_first(self) -> None:
        findings, _ = apply_rules(
            rules_config()["rules"],
            {"asset_resolved": True, "severity": "critical", "asset_criticality": 5,
             "safety_risk": True, "confidence": 0.9, "citation_count": 3,
             "injury_reported": False, "near_miss": False, "environmental_risk": False,
             "in_warranty": False, "spare_required": False},
            "hydraulic_pump",
        )
        priorities = [f.priority for f in findings]
        assert priorities == sorted(priorities)

    def test_message_placeholders_are_interpolated(self) -> None:
        findings, _ = apply_rules(
            rules_config()["rules"],
            {"asset_resolved": True, "severity": "high", "asset_criticality": 4,
             "confidence": 0.9, "citation_count": 2},
            "crane",
        )
        message = next(f.message for f in findings if f.rule_id == "OPS-002")
        assert "{" not in message and "criticality-4" in message
