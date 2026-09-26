# The Performance Coach

The Performance Coach (the Agent coach until the 2026-09 reorganisation,
docs/REORG.md) turns what colleagues say about an agent's work into better
instructions. It is a role agent (`agents/performance-coach/`), always present,
reporting to the Head of People, on a small budget.

## Where its input comes from

- **Feedback** (`pos.feedback`): praise, critique and suggestions any member
  gives any other (`give_feedback`, the task detail, the member's profile).
- **Returned work**: returns are in each task's activity with the reviewer's note.
- **Runs**: tool calls and turns per run (the profile's Recent runs), failed runs.
- **HR**: scores and proposals (`hr_overview`); HR's daily review sends merge and
  revise-instructions proposals to the coach.
- **Shared tools**: HR's weekly report says which shared tools the team used and
  how often they failed.

## The daily loop

1. At 06:30 the job *Repeated critique of an agent → the Agent coach* gives the
   coach one task per agent with two or more open critiques, listing them.
2. The coach looks for the common pattern (feedback, returns, runs).
3. It writes the better version of the agent's instructions and proposes it
   with `propose_instructions(agent, text, reason)`: a task for the Dev agent
   to commit `agents/<slug>/INSTRUCTIONS.md` on `agent/dev`; the deployer checks
   the change (constitution, tests, health) before it reaches `main`.
4. It resolves each piece of feedback: `feedback_resolve(id, "applied",
   applied_ref=<task or commit>)`, or `"dismissed"` with the reason.

The owner, the agent's lead and the coach may propose instructions; the owner,
the coach, the receiver's lead and the receiver may resolve feedback.
