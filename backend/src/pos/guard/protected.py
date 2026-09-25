"""Paths only the owner may change (constitution rules U4 and U5).

This file is itself protected, so an agent cannot shrink the list.
"""

from fnmatch import fnmatchcase

PROTECTED_PATHS: tuple[str, ...] = (
    "docs/CONSTITUTION.md",
    # The enforcement code, hooks and CI check.
    "backend/src/pos/guard/*",  # fnmatch * also matches "/"
    ".github/workflows/constitution.yml",
    "ops/git-hooks/*",
    "ops/owner_allowed_signers",
    # Permissions, limits and budget as config files (owned by the platform core).
    "config/permissions.*",
    "config/limits.*",
    "config/budget.*",
)


def is_protected(path: str) -> bool:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return any(fnmatchcase(path, pattern) for pattern in PROTECTED_PATHS)


def protected_among(paths) -> list[str]:
    return sorted({p for p in paths if is_protected(p)})
