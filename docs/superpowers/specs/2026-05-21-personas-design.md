# JARVIS Personas — Design

**Status:** Draft, awaiting user review
**Date:** 2026-05-21
**Author:** Brainstormed inline; written up by Claude
**Predecessor PRs:** #1–#8 (hardening sprint)
**Successor:** implementation plan (next, via writing-plans skill)

---

## Problem

After eight PRs of hardening, the JARVIS repo has a clear architecture
(`ARCHITECTURE.md`), a documented threat model (`SECURITY.md`), a CI
gate, and branch protection. But the discipline that produced those
PRs lives in one place: the main Claude session. When a future change
touches network/auth/AppleScript/LLM-dispatch surfaces, nothing
*structurally* requires that the threat model gets re-applied. The
quality of every future PR depends on whoever's driving the session
remembering to do the work.

We want named, repeatable personas that encode the disciplines we've
been using inline — so that a security-sensitive change automatically
gets routed through a security review, and a design question gets
routed through an architect, and so on.

We also want this to be *project-specific*: built-in subagent types in
this environment (e.g. `security-scanning:security-auditor`) don't
know about `cwd_allowlist`, `claude_pool`, `audit_log`, the
`untrusted_content` sanitizer, or the contract that the membrane docs
encode. A persona that has read all of `SECURITY.md` and the eight
hardening PRs will produce sharper output than a generic one.

## Goals

1. Five named project-specific personas, each defined as a
   `.claude/agents/<name>.md` file with frontmatter + system prompt.
2. A routing table in `CLAUDE.md` so the main session (and any
   contributor driving a session) knows which persona owns which
   task pattern.
3. A controller persona for tasks that span categories or are
   ambiguous.
4. A tripwire hook that warns (does not block) on edits to the
   canonical 3-file membrane.
5. A concrete first task for the architect: produce a per-module
   verdict (integrate / rewrite / delete) for the orphaned
   self-improvement subsystem (`evolution.py`, `learning.py`,
   `tracking.py`, `ab_testing.py`, `suggestions.py`,
   `conversation.py` — ~1,480 LOC).

## Non-goals

- Runtime integration into JARVIS's voice loop. Personas operate at
  the dev-session layer in this phase. Phase 2 (separate PR) will
  lift them into runtime if useful.
- Hard gates that block edits. Branch protection + CI is the hard
  gate. The hook is a tripwire.
- Touching the orphaned subsystem in this PR. The architect persona
  will analyze it as its first invocation; whatever happens to that
  code is a *later* PR informed by the architect's report.
- A persona for every conceivable specialty. Five is the scoped set.
  More can be added later if a real need shows up.

## The 5 personas

| Persona | Model | Tool whitelist | When to invoke |
|---|---|---|---|
| `security-advisor` | opus | Read, Grep, Bash (read-only), Glob | Any change that touches network / auth / subprocess / AppleScript / LLM-dispatch surfaces, or any membrane file. Returns: go/no-go + STRIDE findings + drift flags against SECURITY.md. |
| `software-architect` | opus | Read, Grep, Glob | Design questions before code is written: module boundaries, refactor scope, "should we integrate X or rewrite." Knows ARCHITECTURE.md + CLAUDE.md + the membrane. Returns: recommendation + tradeoffs + 2–3 alternatives. |
| `code-reviewer` | sonnet | Read, Grep, Bash (`git diff`, read-only) | Before commit/merge. Checks against CLAUDE.md conventions, SECURITY.md rules, PR template. Returns: review comments + must-fix list. |
| `test-runner` | haiku | Bash, Read | Verification pass — runs pytest, pip-audit, reports actual exit code + failures + duration. No interpretation, no synthesis. |
| `controller` | sonnet | Agent (spawns the others), Read, Grep | User explicitly hands off an ambiguous task. Reads the task, decides which persona(s) to invoke + in what order, returns a unified report. |

### Why these models

- **Opus** for security-advisor and software-architect because their
  judgments are load-bearing and a wrong call is expensive.
- **Sonnet** for code-reviewer and controller because synthesis is
  the dominant task.
- **Haiku** for test-runner because its job is fidelity to subprocess
  output, not synthesis. The FULL template the user supplied
  explicitly notes that the test runner should be a separate
  identity from the implementer.

### Why these tool whitelists

Every persona is read-only by default. They produce *reports*, not
code. The main session applies their findings via Edit. This keeps
each persona's blast radius small, makes their output reviewable
before it lands, and prevents any persona from silently mutating the
workspace.

`controller` gets the `Agent` tool exclusively to dispatch — it does
not get Edit, Write, or Bash.

## Routing rules (added to CLAUDE.md)

A new `## Persona Routing` section. The main session reads this and
follows it; humans driving sessions follow the same table.

| Task pattern | Routing |
|---|---|
| Editing `auth.py`, `untrusted_content.py`, `claude_pool.py`, `claude_runner.py`, `cwd_allowlist.py`, `audit_log.py`, `file_perms.py` | Invoke `security-advisor` BEFORE the edit. Apply its findings. |
| Editing `server.py` lines that match `osascript|claude -p|subprocess|JARVIS_SYSTEM_PROMPT|extract_action`, or any `*_access.py` AppleScript builder | `security-advisor` first. |
| New module, removing a module, changing trust boundaries, refactoring across 3+ files | `software-architect` first. |
| Before committing any change with ≥30 LOC diff, or any change that touched the security-sensitive list above | `code-reviewer`. |
| Before claiming "tests pass," "ready to merge," or creating a PR | `test-runner` (separate identity, no synthesis). |
| Task spans multiple categories OR is unclear which persona owns it OR the user asks for "a full review" | `controller`. |
| Routine work that doesn't match any pattern above (docs typos, README edits, comment-only changes) | No persona. Proceed directly. |

### Three principles encoded in the table

1. **Read-only personas, main session applies.** Personas produce
   reports. The session reads them and Edits.
2. **Pre-commit gates are mandatory.** Code-reviewer + test-runner
   before every PR. Branch protection (from PR #4) already enforces
   CI; this adds the human-judgment layer.
3. **The controller is for ambiguity, not bypass.** If the table
   says "security-advisor first," the controller honors that.

### Cost discipline

Opus calls cost real money. The rule is "invoke before the edit," but
the advisor's output gets cached *in the session*: for follow-ups in
the same session, the main session reuses the cached judgment unless
something material changed. This is discipline, not tool enforcement.

## Controller agent design

Normal subagent (`.claude/agents/controller.md`) with one unusual
property: tool whitelist includes `Agent`.

### Job description (its system prompt)

> You are JARVIS's persona controller. Read the task description.
> Decide which of {security-advisor, software-architect, code-reviewer,
> test-runner} should be invoked, in what order, and report their
> combined findings as a unified report.
>
> You do not write code, do not make architectural decisions yourself,
> do not test. You route, sequence, and synthesize.

### Decision algorithm encoded in the prompt

| Task signal | Controller routes to |
|---|---|
| Mentions auth / network / subprocess / AppleScript / LLM-dispatch / membrane file | security-advisor (first, blocking) |
| "Should we…", "How would we…", "Design…", new modules, refactors | software-architect (first, blocking) |
| "Review my changes," "Is this ready," PR mentioned | code-reviewer (after architect/security if they ran) |
| "Did the tests pass," "verify" | test-runner (last, always) |
| Ambiguous, multi-faceted, "full review" | All four: architect → security → reviewer → test-runner |

### Sequential vs parallel

Sequential by default. The reviewer's findings should reflect what
the architect already said. Parallel only when the user explicitly
says "fast pass" — then security + architect run in parallel,
reviewer + test-runner are still sequential at the end.

### Report format

```
## Controller routing report

**Task:** <one-sentence restatement>
**Personas invoked:** security-advisor, code-reviewer (parallel), test-runner (after)

### security-advisor said
<verbatim, no editing>

### code-reviewer said
<verbatim>

### test-runner said
<verbatim>

### Controller synthesis
<2-4 sentences: what to do, in what order, with what caveats>
```

The verbatim passthrough rule matters — controller doesn't filter or
summarize personas. It surfaces them. The synthesis is its own
contribution, clearly fenced so the user can ignore it.

### Constraints

- No recursion (controller cannot invoke itself).
- No writes to disk.
- No contradiction (if security-advisor says "block," controller
  cannot say "ship anyway").
- Capped at the four personas; a fifth specialist requires a design
  conversation, not a controller upgrade.

## Tripwire hook on the membrane

PostToolUse hook (warn, not block) on Edit / Write / MultiEdit to
three canonical files:

| File | Why it's membrane |
|---|---|
| `SECURITY.md` | Trust model + operator's checklist. If this drifts from reality, every other guarantee is suspect. |
| `ARCHITECTURE.md` | Module map + trust boundaries. Renames/restructures must update this. |
| `auth.py` | The token middleware. Any change here changes who can reach JARVIS. |

### Why PostToolUse (warn), not PreToolUse (block)

Blocking edits to security files has a chicken-and-egg problem: the
security-advisor's own recommendations come back as proposed Edits.
A PreToolUse block on `auth.py` blocks the fix too, and the hook
can't tell "this Edit was authorized by the advisor" from "this Edit
bypassed the advisor."

The hard gate already exists: branch protection on `main` requires
CI green + the code-reviewer persona in the routing rules. The hook
adds observability + a reminder, not a second hard gate.

### Hook config (`.claude/settings.json`)

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "scripts/hooks/membrane-edit-warn.sh"
          }
        ]
      }
    ]
  }
}
```

### Hook script (`scripts/hooks/membrane-edit-warn.sh`)

Receives the Tool call JSON on stdin. If `file_path` matches one of
the 3 membrane files, prints a stderr advisory:

```
⚠  Edited SECURITY.md.
⚠  If this change touched the trust model, the security-advisor persona
⚠  should review BEFORE merge. Invoke with:
⚠      Agent(subagent_type='security-advisor')
⚠  This is a tripwire, not a block — discipline lives in CLAUDE.md.
```

Exits 0 either way. Output surfaces to the session as a system
reminder. No fire on Read or Grep — only mutations warn.

### What the hook does NOT do

- Doesn't block. Branch protection + CI + code-reviewer-before-PR
  is the actual gate.
- Doesn't try to detect "was the advisor invoked recently."
  Impossible to do reliably from a hook context.
- Doesn't fire on Read or Grep of membrane files. Reading is fine;
  only mutations warn.
- Doesn't fire on `git revert` (which the hook can't reason about).

## Scope note on the agent files themselves

The full text of each persona's system prompt (the actual content
inside `.claude/agents/<name>.md`) is *implementation detail* —
deliberately not specified in this design doc. The implementation
plan (next, via writing-plans skill) decides:

- The exact wording of each system prompt.
- The checklist that code-reviewer applies to a diff.
- The output format each persona is constrained to.
- Which files each persona pre-loads as context vs reads on demand.

This design doc commits to the *architecture* of the persona system
(names, models, tool whitelists, routing, controller, tripwire), not
to the precise contents of each agent file. That keeps this doc
stable as we iterate on prompt wording without re-approving the
whole spec.

## File deliverables

```
.claude/
  settings.json                      # hook config (PostToolUse warn)
  agents/
    security-advisor.md
    software-architect.md
    code-reviewer.md
    test-runner.md
    controller.md

scripts/hooks/
  membrane-edit-warn.sh              # the tripwire script

CLAUDE.md                            # + new "## Persona Routing" section

docs/superpowers/specs/
  2026-05-21-personas-design.md      # this file

tests/test_personas_setup.py         # smoke tests (see below)
```

Approx scope: ~10 files, ~600–900 lines. Single PR.

## How the dead-code question gets answered

The architect persona's first real invocation:

> "Read `evolution.py`, `learning.py`, `tracking.py`, `ab_testing.py`,
> `suggestions.py`, `conversation.py`. Compare against the current
> architecture (server.py, planner.py, qa.py). Produce a verdict for
> each module: **integrate** (with concrete integration sketch),
> **rewrite-fresh** (with reasoning), or **delete** (with
> justification). Treat them as a coherent subsystem when possible —
> don't recommend keeping half."

Discrete, bounded, read-only. The architect's report becomes the
input to the next PR (which actually decides integrate vs delete vs
rewrite). That replaces my offhand "just delete" recommendation
with focused analysis from a persona designed for exactly this
kind of call.

## Acceptance criteria for the implementation PR

1. CI green: `pytest -q` + `pip-audit -r requirements.txt --strict`.
2. `Agent(subagent_type='security-advisor')` from the main session
   returns a report (not an error). Same for the other four.
3. Editing `auth.py` produces the stderr tripwire; editing
   `server.py` does not.
4. CLAUDE.md's routing section renders correctly + is referenced
   from ARCHITECTURE.md.
5. `tests/test_personas_setup.py` passes — verifies agent files
   parse as YAML/Markdown, frontmatter has required fields, hook
   script is executable and exits 0 on benign inputs.
6. No change to JARVIS runtime behavior. The voice loop, audit log,
   claude_pool, cwd_allowlist, auth middleware are all bit-identical
   pre/post merge.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Opus costs balloon on routine edits to auth.py | Cost-discipline rule in routing section: cache advisor judgment within a session unless material change. |
| Personas drift from SECURITY.md as the project evolves | code-reviewer persona's checklist includes "if SECURITY.md changed, are personas still consistent?" |
| Controller LLM-judgment routes wrong | Routing table in CLAUDE.md is source of truth; controller is convenience for ambiguous handoffs only. Table wins. |
| Hook noise on benign edits | Limited to 3 files. Doesn't fire on Read/Grep. PostToolUse, not PreToolUse — never blocks. |
| Personas hallucinate findings that don't exist | All personas are read-only; their reports are reviewable. Main session decides whether to apply. |
| The first architect invocation produces a verdict the user disagrees with | That's the *point* — a second opinion on the dead-code call. User retains the decision. |

## Reversibility

`git revert` removes everything: agents, hook, CLAUDE.md additions,
spec doc. No runtime artifact persists. The branch is delete-clean.

## Out of scope (deferred to later PRs)

- Phase 2: Runtime integration of personas into JARVIS's voice loop.
- Removing or revising the orphaned subsystem (depends on architect's
  verdict from this PR's first real use).
- Hooks on additional membrane files beyond the canonical 3.
- A user-facing dashboard or telemetry on persona invocations.
- Additional specialist personas (macOS-integration-specialist,
  prompt-engineer, frontend-specialist) — add later if the 5-persona
  set proves insufficient.
