"""Agent registry.

``config/pipeline.yaml`` refers to agents by the keys in :data:`AGENTS`. Adding
a node to the graph means adding a function here and a stanza there -- there is
no third place to remember.
"""

from __future__ import annotations

from .action import dispatch
from .base import Agent, AgentContext, Degraded, Halt
from .decision import approval, guard_output, plan, policy, risk
from .perception import guard_input, intake, redact, translate
from .reasoning import diagnose, resolve_asset, retrieve

AGENTS: dict[str, Agent] = {
    "intake": intake,
    "guard_input": guard_input,
    "redact": redact,
    "translate": translate,
    "resolve_asset": resolve_asset,
    "retrieve": retrieve,
    "diagnose": diagnose,
    "risk": risk,
    "policy": policy,
    "plan": plan,
    "guard_output": guard_output,
    "approval": approval,
    "dispatch": dispatch,
}

__all__ = ["AGENTS", "Agent", "AgentContext", "Degraded", "Halt"]
