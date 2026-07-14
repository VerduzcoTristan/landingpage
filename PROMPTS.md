# PROMPTS.md — copy-paste prompts for agent sessions

Works with any agent (Claude Code, Codex, etc.) as long as AGENTS.md and the
session continuity rules are in the repo. Never explain anything in chat that
belongs in AGENTS.md or PLAN.md — if you find yourself typing context, move it
into a file instead.

---

## 1. Kickoff — plan pass (run once per overhaul/feature)

Use plan mode / read-only mode if the tool has one.

```
Read AGENTS.md. Do the plan pass only. Write the full plan, keep/remove
audit, and decisions to PLAN.md. Do not modify any other files.
```

Then: open PLAN.md, edit it directly (delete steps, change decisions, fix the
audit table). Your edits ARE the approval mechanism — no need to discuss them.

---

## 2. Launch — build pass (after you've edited PLAN.md)

```
PLAN.md is approved as written. Execute it per the session continuity
rules in AGENTS.md. Do not stop to ask questions.
```

---

## 3. Resume — after any cutoff, credit limit, or model/tool switch

```
Read AGENTS.md and PLAN.md, check git status and recent git log, and
continue from the first unchecked step.
```

---

## 4. Course-correct — when a completed step is wrong

Don't debug in chat. Revert and refine:

```
Step N in PLAN.md is wrong: <one sentence on what's wrong>. Revert the
commit(s) for step N, update the step description in PLAN.md so the
mistake cannot recur, then redo it and continue.
```

---

## 5. New feature/task later — reuse the same loop

```
Read AGENTS.md. New task: <one or two sentences>. Do the plan pass only:
append the plan for this task to PLAN.md as a new section with unchecked
steps. Do not modify any other files.
```

Then edit, and launch with prompt #2.

---

## Rules for yourself

- One intervention point per cycle: editing PLAN.md between prompts 1 and 2.
- If you type anything mid-run other than these prompts, that sentence
  belongs in AGENTS.md — add it there so next run doesn't need it.
- After a run finishes, review `git log` and the diff, not the chat
  transcript.
