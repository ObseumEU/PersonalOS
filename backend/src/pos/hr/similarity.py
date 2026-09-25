"""Cheap, deterministic purpose similarity used to spot duplicate agents.

Codex can confirm a match later (the merge proposal becomes a task for the HR agent);
this only has to find candidates without spending tokens.
"""

import re
import unicodedata

# Czech and English filler that says nothing about what an agent does.
_STOP = {
    "an", "and", "the", "of", "for", "to", "in", "on", "with", "by", "from", "that",
    "which", "all", "new", "agent", "agents", "agenta", "agentem",
    "je", "jsou", "ke", "na", "od", "po", "pro", "se", "ve", "ze", "do", "za",
    "ktery", "ktera", "ktere", "nebo",
}


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _stem(word: str) -> str:
    # Rough suffix trim so "emails"/"email" and "e-maily"/"e-mail" meet.
    for suffix in ("ing", "es", "s", "u", "y", "e", "i", "a", "ech", "ove", "ami"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def words(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9]+", _fold(text).replace("-", ""))
    return {_stem(t) for t in tokens if t not in _STOP and len(t) > 1}


def _same(a: str, b: str) -> bool:
    # Czech inflects heavily ("třídí"/"třídění"), so a stem that prefixes the other word counts.
    short, long = sorted((a, b), key=len)
    return short == long or (len(short) >= 4 and long.startswith(short))


def purpose_similarity(a: str, b: str) -> float:
    """Jaccard overlap of purpose words, with prefix matching for inflected forms."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    matched = sum(1 for x in wa if any(_same(x, y) for y in wb))
    return matched / (len(wa) + len(wb) - matched)
