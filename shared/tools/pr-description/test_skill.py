"""Checks the skill file: front matter with name and description, and the sections agents rely on."""

from pathlib import Path

text = (Path(__file__).parent / "SKILL.md").read_text(encoding="utf-8")
assert text.startswith("---\n"), "SKILL.md starts with front matter"
front = text.split("---\n")[1]
assert "name: pr-description" in front
assert "description:" in front
for heading in ("## Shape", "## Rules"):
    assert heading in text, heading
print("ok")
