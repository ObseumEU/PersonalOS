"""The worker loop: wait for work, run it with Codex, take in messages mid-run.

One worker = one agent. It never spends tokens while idle: it long-polls
PersonalOS for its next task or message. While a run is going, it checks its
inbox after every completed step (a safe point):

- stop:        stop the run now (PersonalOS has already paused the agent)
- change_plan: stop at this point and continue the same Codex session with
               the message as the next instruction (`codex exec resume`)
- a chat message addressed to the agent (a DM, an @mention, a reply to it):
               like change_plan, it goes into the running conversation at this
               safe point, with where to answer; the agent replies in that chat
               and adapts its plan (pos_worker.prompt.injection)
- fyi:         remember it and hand it over at the next resume point, or
               before the run finishes
"""

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .client import Blocked, PosClient
from . import pseudo_tools
from .prompt import build_task_prompt, injection, stable_prompt
from .tools import fetch as fetch_tools

log = logging.getLogger("pos_worker")


ALIVE_S = 10  # the alive tick while a run is going (PersonalOS clears the indicator 30 s after the last one)


class _AliveTicker:
    """Tells PersonalOS every ALIVE_S seconds that the run's worker is still there,
    also inside a long tool step: it keeps the chat "working" indicator and the run's
    heartbeat (moved at most once a minute), so the 5-minute silent-run check
    (pos.scheduler.reap_runs) never kills a run whose worker lives. No tokens. It stops
    with the run, so a crashed worker's run is found within 5 minutes."""

    def __init__(self, client, run_id: int, every: float | None = None):
        self.client, self.run_id, self.every = client, run_id, ALIVE_S if every is None else every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"alive-{run_id}", daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.every):
            alive = getattr(self.client, "alive", None)
            if alive:
                alive(self.run_id)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


RETRY_DELAYS = (1, 2, 4, 8, 15, 30, 30, 30)  # ~2 min: PersonalOS restarting (a deploy) or a network blip


def transient(e: Exception) -> bool:
    """A failure worth retrying: PersonalOS unreachable or answering 5xx (not a refusal, not a 4xx)."""
    import httpx

    if isinstance(e, Blocked):
        return False
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code >= 500 or e.response.status_code == 429
    return isinstance(e, (httpx.TransportError, OSError, TimeoutError))


def api_down(e: Exception) -> bool:
    """PersonalOS itself is unreachable or failing (not a local error such as a missing binary)."""
    import httpx

    return isinstance(e, (httpx.TransportError, httpx.HTTPStatusError)) and transient(e)


CHAT_REASONS = ("dm", "mention", "reply", "routed")  # a chat message addressed to the agent: it answers mid-run


def is_chat(m: dict) -> bool:
    return m.get("reason") in CHAT_REASONS and m.get("priority") != "stop"


def step_label(ev: dict, last_tool: str) -> str:
    """What the step that just completed was, for the platform's status snapshot."""
    item = ev.get("item") or {}
    if item.get("command"):
        return f"{item.get('type', 'command')}: {str(item['command'])[:80]}"
    return last_tool or str(item.get("type") or "step")


def tool_of(ev: dict) -> str:
    """The tool a Claude assistant event calls ('' when none)."""
    msg = (ev.get("raw") or {}).get("message")
    for c in (msg.get("content") if isinstance(msg, dict) else None) or []:
        if isinstance(c, dict) and c.get("type") == "tool_use":
            return str(c.get("name") or "")[:80]
    return ""


# The model wrote its tool calls as text (pos_worker.pseudo_tools): nothing ran. One more try in
# the same session with this correction; a second time the run fails (PersonalOS escalates it).
FAKE_TOOLS_RETRY = (
    "Your last answer contained tool calls written as text (function_calls / invoke / parameter markup). "
    "They were NOT executed: nothing ran and nothing of it happened. Do the work with your real tools "
    "(the tool-use interface you were given), then report what they actually returned. If the tool you "
    "need is not available to you, say so plainly (name the tool) instead of writing the call as text.")


OWNER_STEP_FACTOR = 2  # an owner-assigned task without max_steps_owner gets twice the agent's cap


def step_cap(profile: dict, task: dict, default: int) -> int:
    """This task's step cap: the agent's own (agent.json profile.max_steps, else the worker's
    WORKER_MAX_STEPS); a task the owner asked for himself gets profile.max_steps_owner, else twice
    that. 0 = no cap."""
    try:
        base = int(profile.get("max_steps") or default or 0)
    except (TypeError, ValueError):
        base = default or 0
    if not base or not task.get("owner_request"):
        return base
    try:
        return max(base, int(profile.get("max_steps_owner") or base * OWNER_STEP_FACTOR))
    except (TypeError, ValueError):
        return base * OWNER_STEP_FACTOR


class Worker:
    def __init__(self, client: PosClient, new_session: Callable[[str, str | None, dict], object], *, poll_wait: int = 60,
                 max_resumes: int = 12, max_steps: int = 0, sleep: Callable[[float], None] = time.sleep,
                 tools_dir: str | None = None, triage: Callable[[dict, dict], dict | None] | None = None,
                 exit_idle_s: float = 0, clock: Callable[[], float] = time.monotonic):
        self.client = client
        # The agent pool starts a worker when its agent has a task; it ends itself after
        # this long without one (0 = never), so an idle agent costs no process at all.
        self.exit_idle_s = exit_idle_s
        self.clock = clock
        # The cheap check before a full run (pos_worker.triage): (me, task) -> verdict or None.
        self.triage = triage
        self.new_session = new_session
        self.poll_wait = poll_wait
        self.max_resumes = max_resumes
        self.max_steps = max_steps  # 0 = no cap; else a runaway run stops and the task goes back
        self.base_max_steps = max_steps  # the worker's default (WORKER_MAX_STEPS); each task sets its own cap
        self.sleep = sleep
        self.tools_dir = Path(tools_dir) if tools_dir else Path.cwd()  # where tool files are found
        self.context: list[dict] = []  # messages received while idle, used in the next task
        self.me: dict = {}

    # ------------------------------------------------------------- calls to PersonalOS

    def _call(self, fn: Callable, *args, delays=RETRY_DELAYS, **kw):
        """A call to PersonalOS that survives a short outage (a deploy restarts the API):
        retried with a growing pause on transient errors; others raise at once."""
        for i, pause in enumerate((*delays, None)):
            try:
                return fn(*args, **kw)
            except Exception as e:  # noqa: BLE001
                if pause is None or not transient(e):
                    raise
                log.info("PersonalOS call %s failed (%s); retry %d in %ss",
                         getattr(fn, "__name__", "?"), str(e)[:120], i + 1, pause)
                self.sleep(pause)

    def _safe(self, fn: Callable, default, *args):
        """_call inside a run: after the retries, log it and go on with `default`."""
        try:
            return self._call(fn, *args)
        except Exception as e:  # noqa: BLE001 - the run goes on; the next step tries again
            if not transient(e):
                raise
            log.warning("PersonalOS unreachable (%s): the run goes on", str(e)[:120])
            return default

    # ------------------------------------------------------------- main loop

    IDLE = ("idle", "read_messages", "held", "blocked", "skipped", "api_down")  # nothing ran: the idle clock keeps going

    def run_forever(self) -> str:
        """Returns why it ended (exit_idle_s only): "idle", or "blocked" when its run was refused
        (the pool then gives the slot to another agent for a while, pos_worker.pool.BLOCKED_EXIT)."""
        self.me = self.client.me()
        log.info("worker for %s started", self.me["name"])
        busy_at = self.clock()
        while True:
            what = self.step()
            if what == "blocked" and self.exit_idle_s:
                log.info("worker for %s: run refused; exiting so another agent gets the slot", self.me.get("name"))
                return "blocked"
            if what not in self.IDLE:
                busy_at = self.clock()
            elif self.exit_idle_s and self.clock() - busy_at > self.exit_idle_s:
                log.info("worker for %s idle for %ss: exiting (the pool starts it again on work)",
                         self.me.get("name"), int(self.exit_idle_s))
                return "idle"

    def step(self) -> str:
        """One iteration; returns what happened (for tests and logs)."""
        try:
            return self._step()
        except Exception as e:  # noqa: BLE001 - a bad run must never kill the worker
            if api_down(e):  # PersonalOS down (a redeploy drops the idle poll): one line, no traceback
                log.warning("PersonalOS unreachable (%s); trying again", str(e)[:120] or type(e).__name__)
                self.sleep(10)
                return "api_down"
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
        # Fresh for every task: grants change (pos.access), and an agent may answer the person
        # waiting for it in chat only while that task is open (pos.chat.may_answer).
        try:
            self.me = self.client.me()
        except Exception:  # noqa: BLE001 - PersonalOS hiccup: the last known one will do
            if not self.me:
                raise
        self.max_steps = step_cap(self.me.get("profile") or {}, task, self.base_max_steps)
        ref = task["ref"]
        # Ask for the run first: if the kill switch or the budget says no, the
        # task stays in the queue untouched instead of hanging in "working".
        try:
            started = self.client.start_run(ref)
        except Blocked as e:
            log.info("run for %s blocked: %s", ref, e)
            if not self.exit_idle_s:  # the pool's worker ends at once instead (run_forever)
                self.sleep(min(self.poll_wait, 30))
            return "blocked"
        try:
            run_id = started["run_id"]
            self.client.claim(ref, run_id)
        except Exception as e:  # someone else took it, or it changed meanwhile
            self._call(self.client.finish_run, started["run_id"], "cancelled", "", f"could not claim {ref}: {e}")
            return "skipped"

        # The alive tick covers the whole run, from the claim to the finish (the cheap check, the
        # session's start, the hand-in too): PersonalOS declares a run dead after 5 silent minutes.
        with _AliveTicker(self.client, run_id):
            return self._claimed(ref, task, run_id, started)

    def _claimed(self, ref: str, task: dict, run_id: int, started: dict) -> str:
        engine = started.get("engine") or "codex"
        # The agent's max USD per run from PersonalOS (pos.access); None when it has none.
        self.run_cap_usd = started.get("max_budget_usd")
        check = self._triage(ref, task, run_id)
        if check and check.get("action") in ("parked", "handed_back"):
            self._call(self.client.finish_run, run_id, "ok", check.get("jsonl", ""),
                       f"triage: {check['verdict']}, {check['action'].replace('_', ' ')}")
            log.info("%s: triage said %s, no full run", ref, check["verdict"])
            return "triaged"
        if started.get("knowledge"):  # the knowledge base's passages for this task (pos.knowledge_first)
            task = {**task, "knowledge": started["knowledge"]}
        try:
            return self._run_task(ref, task, run_id, engine, started.get("model"), check)
        except Exception as e:  # noqa: BLE001 - report it, hand the task back, keep the worker alive
            log.exception("run %s for %s failed", run_id, ref)
            try:
                self._call(self.client.finish_run, run_id, "error", "", f"worker error: {e}"[:2000])
                self._call(self.client.handback, ref, f"worker error: {e}"[:400])
            except Exception:  # noqa: BLE001
                log.exception("could not report the failure")
            return "error"

    def _triage(self, ref: str, task: dict, run_id: int) -> dict | None:
        """Ask the cheap check, tell PersonalOS what it said. Any failure: run as usual."""
        if not self.triage:
            return None
        try:
            check = self.triage(self.me, task)
            if not check:
                return None
            res = self.client.triage(ref, {"verdict": check["verdict"], "size": check.get("size"),
                                           "question": check.get("question", ""), "run_id": run_id})
            return {**check, "action": res.get("action", "run")}
        except Exception:  # noqa: BLE001 - fail-open: the check must never cost the task its run
            log.exception("%s: triage failed; running as usual", ref)
            return None

    def _run_task(self, ref: str, task: dict, run_id: int, engine: str, model: str | None,
                  check: dict | None = None) -> str:
        # The agent's tools (personal and shared); none if PersonalOS cannot say.
        me = {**self.me, "tools": fetch_tools(self.client, self.tools_dir), "task_ref": ref,
              "feedback": self.client.feedback(), "memory": self.client.memory(), "max_budget_usd": getattr(self, "run_cap_usd", None),
              "run_id": run_id}
        if check and check.get("size"):
            me["size"] = check["size"]  # effort and cost cap follow it (new_session)
            me["size_hint"] = f"A first check sized this task {check['size']}; keep to that size's budget."
        elif task.get("run_size") in ("S", "M", "L"):
            # PersonalOS sized the run itself (a routine review packet: pos.review_packet): low effort.
            me["size"] = task["run_size"]
        claude = engine == "claude"
        if claude:
            me["stable_prompt"] = stable_prompt(me)
        session = self.new_session(engine, model, me)
        # Claude takes the constitution and the stable part as its system prompt (cached
        # across runs); Codex gets both at the top of the prompt.
        prompt = build_task_prompt(me, task, self.context, include_guardrails=not claude, include_stable=not claude)
        self.context = []
        outcome = self._session_loop(session, prompt, run_id, ref)  # inside handle_task's alive tick
        return self._after_run(ref, run_id, engine, session, check, outcome)

    def _session_loop(self, session, prompt: str, run_id: int, ref: str) -> str:
        pending_fyi: list[dict] = []
        resumes = 0
        steps = 0
        outcome = "ok"
        last_tool = ""
        fake_retried = False
        while True:
            interrupted = None
            for ev in session.run(prompt):
                if ev.get("type") == "agent_message":
                    last_tool = tool_of(ev) or last_tool
                if ev.get("type") != "item.completed":
                    continue
                # Safe point: the last step is complete.
                steps += 1
                if self.max_steps and steps > self.max_steps:
                    session.stop()
                    interrupted = "step_cap"
                    break
                # A short outage (a deploy) must not end the run: retried, and when PersonalOS
                # stays away the run carries on to the next step instead of dying.
                state = self._safe(self.client.heartbeat, {}, run_id, step_label(ev, last_tool), steps)
                if state.get("run_cancelled") or state.get("frozen") or state.get("paused"):
                    session.stop()
                    interrupted = "cancelled"
                    break
                msgs = self._safe(self.client.inbox, [], run_id)
                if any(m["priority"] == "stop" for m in msgs):
                    session.stop()
                    interrupted = "cancelled"
                    break
                urgent = [m for m in msgs if m["priority"] == "change_plan" or is_chat(m)]
                pending_fyi += [m for m in msgs if m["priority"] == "fyi" and not is_chat(m)]
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
                                  f"{pseudo_tools.clean(session.last_message)[:300] or 'none'}")
                break
            if interrupted == "inject":
                resumes += 1
                log.info("%s: injected new instructions into the running session (resume %d)", ref, resumes)
                continue
            if session.failed:
                outcome = "error"
                break
            if pseudo_tools.contains(session.last_message):
                if not fake_retried:
                    fake_retried = True
                    log.warning("%s: the model wrote tool calls as text; nothing ran. Retrying once", ref)
                    prompt = FAKE_TOOLS_RETRY
                    continue
                outcome = "error"
                session.failed = f"{pseudo_tools.MARKER} (twice, after one retry)"
                session.last_message = pseudo_tools.clean(session.last_message)
                break
            # The session finished its turn. Late information still gets a say.
            pending_fyi += [m for m in self._safe(self.client.inbox, [], run_id) if m["priority"] != "stop"]
            if pending_fyi and resumes < self.max_resumes:
                prompt = injection(pending_fyi, before_finishing=True)
                pending_fyi = []
                resumes += 1
                continue
            break
        return outcome

    def _after_run(self, ref: str, run_id: int, engine: str, session, check: dict | None, outcome: str) -> str:
        # The check's usage line first, so PersonalOS counts its cost with the run's.
        jsonl = "\n".join(x for x in ((check or {}).get("jsonl", ""), session.jsonl) if x)
        done = self._call(self.client.finish_run, run_id, outcome, jsonl,
                          session.failed or ("stopped" if outcome == "cancelled" else ""),
                          delays=(*RETRY_DELAYS, 60, 60, 60))  # the result is worth a longer wait
        if done.get("requeued"):  # the runtime hit its usage limit; PersonalOS retries on the other one
            log.info("%s: %s hit its usage limit, task requeued", ref, engine)
            return "requeued"
        now = self._call(self.client.task, ref)
        if now.get("assignee_id") not in (None, self.me.get("id")):
            # Reassigned to someone else meanwhile: it is theirs now, leave it alone.
            log.info("%s: reassigned to %s, not handing anything in", ref, now.get("assignee_name"))
            return "reassigned"
        if outcome == "ok":
            # The agent may have handed it in itself (complete_task over MCP).
            if now["status"] == "working":
                self._call(self.client.complete, ref, session.last_message[:2000] or "Done.")
        elif outcome == "error":
            self._call(self.client.handback, ref, session.failed[:400])
        return outcome
