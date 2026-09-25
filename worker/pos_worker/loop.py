"""The worker loop: wait for work, run it with Codex, take in messages mid-run.

One worker = one agent. It never spends tokens while idle: it long-polls
PersonalOS for its next task or message. While a run is going, it checks its
inbox after every completed step (a safe point):

- stop:        stop the run now (PersonalOS has already paused the agent)
- change_plan: stop at this point and continue the same Codex session with
               the message as the next instruction (`codex exec resume`)
- fyi:         remember it and hand it over at the next resume point, or
               before the run finishes
"""

import logging
import time
from collections.abc import Callable

from .client import Blocked, PosClient
from .prompt import build_task_prompt, injection

log = logging.getLogger("pos_worker")


class Worker:
    def __init__(self, client: PosClient, new_session: Callable[[str, str | None, dict], object], *, poll_wait: int = 60,
                 max_resumes: int = 6, max_steps: int = 0, sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.new_session = new_session
        self.poll_wait = poll_wait
        self.max_resumes = max_resumes
        self.max_steps = max_steps  # 0 = no cap; else a runaway run stops and the task goes back
        self.sleep = sleep
        self.context: list[dict] = []  # messages received while idle, used in the next task
        self.me: dict = {}

    # ------------------------------------------------------------- main loop

    def run_forever(self) -> None:
        self.me = self.client.me()
        log.info("worker for %s started", self.me["name"])
        while True:
            self.step()

    def step(self) -> str:
        """One iteration; returns what happened (for tests and logs)."""
        try:
            return self._step()
        except Exception:  # noqa: BLE001 - a bad run must never kill the worker
            log.exception("step failed; carrying on")
            self.sleep(10)
            return "crashed"

    def _step(self) -> str:
        work = self.client.next_work(self.poll_wait)
        state = work.get("state", {})
        if state.get("frozen") or state.get("paused") or state.get("archived"):
            self.sleep(min(self.poll_wait, 30))
            return "held"
        if work.get("task"):
            return self.handle_task(work["task"])
        if work.get("unread_messages"):
            # Idle: read them now (no tokens), use them as context for the next task.
            self.context.extend(self.client.inbox())
            return "read_messages"
        return "idle"

    # ------------------------------------------------------------- one task

    def handle_task(self, task: dict) -> str:
        if not self.me:
            self.me = self.client.me()
        ref = task["ref"]
        # Ask for the run first: if the kill switch or the budget says no, the
        # task stays in the queue untouched instead of hanging in "working".
        try:
            started = self.client.start_run(ref)
        except Blocked as e:
            log.info("run for %s blocked: %s", ref, e)
            self.sleep(min(self.poll_wait, 30))
            return "blocked"
        try:
            run_id = started["run_id"]
            self.client.claim(ref, run_id)
        except Exception as e:  # someone else took it, or it changed meanwhile
            self.client.finish_run(started["run_id"], "cancelled", "", f"could not claim {ref}: {e}")
            return "skipped"

        engine = started.get("engine") or "codex"
        try:
            return self._run_task(ref, task, run_id, engine, started.get("model"))
        except Exception as e:  # noqa: BLE001 - report it, hand the task back, keep the worker alive
            log.exception("run %s for %s failed", run_id, ref)
            try:
                self.client.finish_run(run_id, "error", "", f"worker error: {e}"[:2000])
                self.client.handback(ref, f"worker error: {e}"[:400])
            except Exception:  # noqa: BLE001
                log.exception("could not report the failure")
            return "error"

    def _run_task(self, ref: str, task: dict, run_id: int, engine: str, model: str | None) -> str:
        session = self.new_session(engine, model, self.me)
        # Claude takes the constitution as a system prompt; Codex gets it at the top of the prompt.
        prompt = build_task_prompt(self.me, task, self.context, include_guardrails=engine != "claude")
        self.context = []
        pending_fyi: list[dict] = []
        resumes = 0
        steps = 0
        outcome = "ok"
        while True:
            interrupted = None
            for ev in session.run(prompt):
                if ev.get("type") != "item.completed":
                    continue
                # Safe point: the last step is complete.
                steps += 1
                if self.max_steps and steps > self.max_steps:
                    session.stop()
                    interrupted = "step_cap"
                    break
                state = self.client.heartbeat(run_id)
                if state.get("run_cancelled") or state.get("frozen") or state.get("paused"):
                    session.stop()
                    interrupted = "cancelled"
                    break
                msgs = self.client.inbox(run_id)
                if any(m["priority"] == "stop" for m in msgs):
                    session.stop()
                    interrupted = "cancelled"
                    break
                urgent = [m for m in msgs if m["priority"] == "change_plan"]
                pending_fyi += [m for m in msgs if m["priority"] == "fyi"]
                if urgent and resumes < self.max_resumes:
                    session.stop()
                    prompt = injection(urgent + pending_fyi)
                    pending_fyi = []
                    interrupted = "inject"
                    break
            if interrupted == "cancelled":
                outcome = "cancelled"
                break
            if interrupted == "step_cap":
                outcome = "error"
                session.failed = (f"step limit reached ({self.max_steps} steps); last note: "
                                  f"{session.last_message[:300] or 'none'}")
                break
            if interrupted == "inject":
                resumes += 1
                log.info("%s: injected new instructions into the running session (resume %d)", ref, resumes)
                continue
            if session.failed:
                outcome = "error"
                break
            # The session finished its turn. Late information still gets a say.
            pending_fyi += [m for m in self.client.inbox(run_id) if m["priority"] != "stop"]
            if pending_fyi and resumes < self.max_resumes:
                prompt = injection(pending_fyi, before_finishing=True)
                pending_fyi = []
                resumes += 1
                continue
            break

        done = self.client.finish_run(run_id, outcome, session.jsonl,
                                      session.failed or ("stopped" if outcome == "cancelled" else ""))
        if done.get("requeued"):  # the runtime hit its usage limit; PersonalOS retries on the other one
            log.info("%s: %s hit its usage limit, task requeued", ref, engine)
            return "requeued"
        if outcome == "ok":
            # The agent may have handed it in itself (complete_task over MCP).
            if self.client.task(ref)["status"] == "working":
                self.client.complete(ref, session.last_message[:2000] or "Done.")
        elif outcome == "error":
            self.client.handback(ref, session.failed[:400])
        return outcome
