"""Constitution and guardrails (docs/CONSTITUTION.md, AGENTS-SPEC.md 5.1-5.2).

A self-contained layer the platform core calls into. It never imports the core;
the core passes in who is acting and what they want to do.
"""

from .rules import RULES, Actor, ConstitutionViolation, Decision, Rule

__all__ = ["RULES", "Actor", "ConstitutionViolation", "Decision", "Rule"]
