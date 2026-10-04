"""Agent evals: catch regressions in the agents' behaviour when their prompts change.

Scenarios (pos.evals.scenarios, 2 per role: CEO, Head of Customer Success, Kniha Lead, Software Engineer,
SRE, Access manager) give an agent one task, as the worker would, and score its run with deterministic
named checks (pos.evals.checks): the right tools called (a task or handoff to the right member), no task
for the owner, no raw ids (T-123, chunk ids, numeric ids) in what the owner or a customer reads, an
'Ověřeno:' line on the hand-in (plus a commit sha or numbers where relevant), Czech for people-facing
text, length limits, nothing forbidden (money promised to a customer, a budget raised for a looping
agent, a git push). Score = checks passed / total, per scenario and per role.

Two engines:
- mock (default, deterministic, free): each scenario's scripted good transcript is scored, and every
  bad variant must fail the check it was written to break. This tests the checks themselves; CI runs it
  through backend/tests/test_evals.py.
- live (--live, manual only, costs money): the local `claude` CLI runs the real agent exactly as the
  worker does (pos_worker.claude.ClaudeSession, stable_prompt + build_task_prompt, the agent's
  agents/<slug> instructions, profile, model and effort, the real guardrails) against a mock pos MCP
  server (pos.evals.mock_server: the real tool schemas, canned per-scenario answers, every call
  recorded), in a throw-away work folder (a tiny git repository for the Software Engineer and the
  Kniha Lead). Each run is capped with --max-budget-usd; the cost comes from the CLI's result event.

How to use it around a prompt change (agents/<slug>/INSTRUCTIONS.md, worker/pos_worker/prompt.py, the
constitution or the guardrails), from backend/ with PYTHONPATH=src (CI installs the package):

    python -m pos.evals --all                       # mock: the checks still work (seconds, free)
    python -m pos.evals --role ceo --live           # baseline BEFORE the change: one role, all its scenarios
    python -m pos.evals --role ceo --live --scenario ceo-card-payments --max-budget-usd 0.5
    ... change the prompt ...
    python -m pos.evals --role ceo --live           # AFTER: compare checks passed, cost and turns
    python -m pos.evals --all --live --total-budget-usd 2 --json > after.json

A live run's prompt, stream, recorded calls and transcript are kept under --out (default: a new temp
folder, printed). Models are not compared one run each: a single live run is a sample, so rerun the
scenario that changed before concluding.
"""
