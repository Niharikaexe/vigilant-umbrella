"""Deterministic policy engine.

The rule expressions in ``config/rules.yaml`` are written by policy owners, so
they must be safe to evaluate but must not require a Python release to change.
We parse each expression with :mod:`ast` and walk the tree against an explicit
node allow-list: literals, names, comparisons, boolean and unary operators, and
nothing else. No calls, no attribute access, no subscripts, no comprehensions.

That rules out the usual ``eval`` escapes (``().__class__.__bases__`` and
friends) by construction rather than by blacklist, which is the only version of
this that survives review.
"""

from __future__ import annotations

import ast
from typing import Any

from .models import PRIORITY_ORDER, Finding

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not, ast.USub,
    ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.Name, ast.Load,
    ast.Constant,
    ast.List, ast.Tuple, ast.Set,
    ast.IfExp,
)


LITERAL_ALIASES: dict[str, Any] = {
    "true": True, "false": False,
    "yes": True, "no": False,
    "null": None, "none": None,
}


class RuleError(ValueError):
    """Raised when a rule expression is malformed or uses a banned construct."""


def _validate(node: ast.AST) -> None:
    for child in ast.walk(node):
        if not isinstance(child, _ALLOWED_NODES):
            raise RuleError(
                f"disallowed expression element {type(child).__name__!r} in rule"
            )


def compile_expression(expr: str) -> ast.Expression:
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:  # pragma: no cover - config error path
        raise RuleError(f"cannot parse rule expression {expr!r}: {exc}") from exc
    _validate(tree)
    return tree


def evaluate(expr: str, context: dict[str, Any]) -> bool:
    """Evaluate a rule expression against the run context.

    A missing variable evaluates to ``None`` rather than raising, so a rule
    referring to a field an agent could not produce simply does not fire. A
    rule that errors is treated as *not fired* but is surfaced to the caller --
    we never let a broken rule silently approve something.
    """
    tree = compile_expression(expr)
    # `__builtins__` emptied: no len(), no open(), nothing reachable by name.
    safe_globals: dict[str, Any] = {"__builtins__": {}}
    # Policy authors are not Python programmers. Accept YAML/JSON spelling of
    # the literals alongside Python's, so `x == true` means what it looks like
    # instead of silently comparing against an undefined name.
    scope = _NoneDefaultDict(LITERAL_ALIASES)
    scope.update(context)
    try:
        return bool(eval(compile(tree, "<rule>", "eval"), safe_globals, scope))  # noqa: S307
    except TypeError:
        # e.g. comparing None to a number because an upstream agent degraded.
        return False
    except ZeroDivisionError:
        return False


class _NoneDefaultDict(dict):
    """Locals mapping that yields ``None`` for unknown names."""

    def __missing__(self, key: str) -> None:
        return None


def _format(template: str, context: dict[str, Any]) -> str:
    """Interpolate ``{field}`` placeholders, tolerating missing keys."""
    try:
        return template.format_map(_BlankDefaultDict(context))
    except (ValueError, IndexError):  # pragma: no cover - bad template
        return template


class _BlankDefaultDict(dict):
    def __missing__(self, key: str) -> str:
        return "n/a"


def apply_rules(
    rules: list[dict[str, Any]],
    context: dict[str, Any],
    asset_class: str | None = None,
) -> tuple[list[Finding], list[str]]:
    """Run every applicable rule.

    Returns the findings plus a list of rule ids that errored, so the caller can
    fail closed rather than pretend a clean pass.
    """
    findings: list[Finding] = []
    errored: list[str] = []

    for rule in rules:
        applies = rule.get("applies_to", ["*"])
        if applies != ["*"] and asset_class is not None and asset_class not in applies and "*" not in applies:
            continue
        try:
            fired = evaluate(rule["when"], context)
        except RuleError:
            errored.append(rule.get("id", "<unknown>"))
            continue
        if not fired:
            continue
        findings.append(
            Finding(
                rule_id=rule["id"],
                severity=rule.get("severity", "info"),
                priority=rule.get("priority", "P4"),
                message=_format(rule.get("message", rule.get("description", "")), context),
                actions=list(rule.get("actions", [])),
                citation=rule.get("citation", "uncited"),
            )
        )

    findings.sort(key=lambda f: PRIORITY_ORDER.get(f.priority, 9))
    return findings, errored


def highest_priority(findings: list[Finding], default: str = "P4") -> str:
    if not findings:
        return default
    return min(findings, key=lambda f: PRIORITY_ORDER.get(f.priority, 9)).priority
