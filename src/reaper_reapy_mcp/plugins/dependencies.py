"""Dependency finding evaluation (contract 05 §2.6, 10 §3).

Rules are provider data plus an optional provider evaluator; Core evaluates
them against the **projected** world (live state overlaid with the planned
changes), because judging against the raw snapshot falsely refuses a correct
patch. A ``hard`` finding that is not informational refuses before any write.
"""

from __future__ import annotations

from typing import Any, Sequence

from .errors import DependencyViolation
from .model import Finding
from .provider import DependencyRuleSet

__all__ = ["evaluate", "hard_findings", "raise_on"]

_OPERATORS = {
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "in": lambda a, b: a in b,
    "truthy": lambda a, _b: bool(a),
}


def evaluate(ruleset: DependencyRuleSet, world: dict[str, Any]) -> list[Finding]:
    """Every finding the rule set produces for the projected world."""
    if ruleset.evaluate is not None:
        return list(ruleset.evaluate(world, ruleset.rules))
    findings: list[Finding] = []
    for rule in ruleset.rules:
        when = rule.get("when") or {}
        signal = when.get("signal")
        operator = str(when.get("op") or "truthy")
        expected = when.get("value")
        if signal is None:
            continue
        comparator = _OPERATORS.get(operator)
        if comparator is None:
            continue
        observed = world.get(signal)
        if comparator(observed, expected):
            findings.append(
                Finding(
                    kind=str(rule.get("kind") or "hard"),
                    rule=str(rule.get("id") or "rule"),
                    subject={"signal": signal},
                    message=str(rule.get("message") or ""),
                    detail={"observed": observed, "expected": expected, "op": operator},
                    informational=bool(rule.get("informational", False)),
                )
            )
    return findings


def hard_findings(findings: Sequence[Finding]) -> list[Finding]:
    """``hard`` findings that are not informational refuse the operation."""
    return [finding for finding in findings if finding.kind == "hard" and not finding.informational]


def raise_on(findings: Sequence[Finding]) -> None:
    """Raise ``DependencyViolation`` with the finding rows (never prose only).

    Two situations reach here and they are **not** the same claim, so the message
    and each row say which one it is:

    - the rule's condition was *decided and unmet* (``evaluated`` is not False);
    - the rule could **not be evaluated** and the plan entered its subject
      (``detail.evaluated is False``) - the layer refuses rather than reporting a
      check it did not perform (Decision 1).
    """
    blocking = hard_findings(findings)
    if not blocking:
        return
    unevaluable = [finding for finding in blocking if finding.detail.get("evaluated") is False]
    label = (
        "unevaluable hard dependency rule(s)" if unevaluable else "unmet hard dependency rule(s)"
    )
    raise DependencyViolation(
        label + ": " + "; ".join(finding.rule for finding in blocking),
        detail={
            "findings": [
                {
                    "rule": finding.rule,
                    "kind": finding.kind,
                    "message": finding.message,
                    "subject": finding.subject,
                    "detail": dict(finding.detail),
                    "informational": finding.informational,
                    "evaluated": finding.detail.get("evaluated", True),
                    "reason": finding.detail.get("reason"),
                }
                for finding in blocking
            ],
            "unevaluable": [finding.rule for finding in unevaluable],
        },
    )
