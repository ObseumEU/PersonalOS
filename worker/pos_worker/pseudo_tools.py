"""Tool calls written as text ("pseudo tool calls"); the same check as PersonalOS's pos.pseudo_tools
(the worker runs in its own container, without the backend package).

A model without working tools, or one that loses track of them, sometimes writes the
tool-call markup of its training format as plain text: `<function_calls>`, `<invoke
name="bash">`, `<parameter name="command">…`, with or without an `antml:` prefix. Nothing
runs; the text only looks like work. Seen 2026-09-27: the task summary model (a tool-less
Haiku call) answered T-215's summary as if it were the assignee, with a fake `git clone`.

- `contains(text)`: does an agent-authored text carry such markup (outside code)?
- `clean(text)`: the text with each block replaced by a short "[nástroj: bash …]" marker
  (or removed with `marker=False`); what the UI, the chat and the summary model see.

Markup inside Markdown code (``` fences or `inline` spans) is left alone: an engineer may
quote it on purpose.
"""

import re

MARKER = "the model wrote tool calls as text: the tools didn't run"

_P = r"(?:antml:)?"
_NAMES = r"(?:function_calls|function_results|function_result|tool_calls?|tool_use|tool_results?)"
# One block: from an opening wrapper to its end (or the end of the text, when it was cut off).
_BLOCK = re.compile(rf"<{_P}(function_calls|tool_calls?|tool_use)\b[^>]*>[\s\S]*?(?:</{_P}\1\s*>|\Z)", re.I)
_RESULTS = re.compile(rf"<{_P}(function_results?|tool_results?)\b[^>]*>[\s\S]*?(?:</{_P}\1\s*>|\Z)", re.I)
_INVOKE = re.compile(rf"<{_P}invoke\s+name\s*=\s*[\"']?([\w.:-]*)[^>]*>[\s\S]*?(?:</{_P}invoke\s*>|\Z)", re.I)
_PARAM = re.compile(rf"<{_P}parameter\s+name\s*=[^>]*>[\s\S]*?(?:</{_P}parameter\s*>|\Z)", re.I)
# Stray tags left over (a closing tag alone, a tag cut off at the end: "</fun…").
_STRAY = re.compile(rf"</?{_P}(?:{_NAMES}|invoke|parameter)\b[^>]*>?|</?{_P}(?:fun|func|funct|functi|functio|function|"
                    rf"function_|inv|invo|invok|par|para|param|parame|paramet|paramete)…?$", re.I)
_DETECT = re.compile(rf"<\s*/?\s*{_P}(?:{_NAMES}\b|invoke\s+name\s*=|parameter\s+name\s*=)", re.I)
# Markdown code: fenced blocks and inline spans (kept as they are).
_CODE = re.compile(r"(```[\s\S]*?(?:```|\Z)|`[^`\n]*`)")


def _outside_code(text: str) -> list[tuple[bool, str]]:
    """[(is_code, part)] in order."""
    parts = _CODE.split(text or "")
    return [(i % 2 == 1, p) for i, p in enumerate(parts) if p]


def contains(text: str | None) -> bool:
    """Does the text carry tool-call markup outside Markdown code?"""
    if not text or "<" not in text:
        return False
    return any(not code and _DETECT.search(part) for code, part in _outside_code(text))


def _tool_of(block: str) -> str:
    m = re.search(rf"<{_P}invoke\s+name\s*=\s*[\"']?([\w.:-]+)", block, re.I)
    return (m[1] if m else "").removeprefix("mcp__")[:40]


def chip(tool: str) -> str:
    return f"[nástroj: {tool} …]" if tool else "[nástroj …]"


def _clean_part(part: str, marker: bool) -> str:
    def block(m: re.Match) -> str:
        return f" {chip(_tool_of(m[0]))} " if marker else " "

    out = _BLOCK.sub(block, part)
    out = _RESULTS.sub(" ", out)
    out = _INVOKE.sub(block, out)
    out = _PARAM.sub(" ", out)
    out = _STRAY.sub(" ", out)
    return out


def clean(text: str | None, marker: bool = True) -> str:
    """The text without tool-call markup (outside code); each call becomes a short marker."""
    if not contains(text):
        return text or ""
    out = "".join(part if code else _clean_part(part, marker) for code, part in _outside_code(text or ""))
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"(\[nástroj[^\]]*\]\s*)(\s*\[nástroj[^\]]*\])+", r"\1", out)  # one marker per run of calls
    return re.sub(r"\n{3,}", "\n\n", out).strip()
