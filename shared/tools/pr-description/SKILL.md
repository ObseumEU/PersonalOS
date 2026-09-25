---
name: pr-description
description: Write a short, reviewable description of a change (commit message body, merge note or task hand-in).
---

# Describing a change

Use this when you hand in work: the commit message body, the note on
`complete_task`, or a merge description. The owner reads many of these;
make each one quick to check.

## Shape

1. **Title** (one line, imperative, under 70 characters): what the change does.
   "Add usage counts to the Tools page", not "Tools changes".
2. **Why** (1-2 sentences): the task or problem it answers. Name the task ref
   (T-123) when there is one.
3. **What changed** (3-6 bullets): by area, not by file. Mention anything
   removed or archived, and any migration.
4. **How it was checked**: the exact test command and its result, the build,
   anything you tried by hand. Say plainly what you did not check.
5. **Look at**: the one or two places where a reviewer's attention pays off
   (a tricky condition, a guard, a changed default).

## Rules

- Plain English, short sentences, no marketing words.
- Facts only: do not claim tests passed unless you ran them in this run.
- No secrets, keys or personal data in the description.
- If the change is partial, say what is left as a follow-up task.
- Add the trailer `Agent: <your name>` to commit messages.

You can run the shared `summarize-diff` script first to list the changed
files and line counts.
