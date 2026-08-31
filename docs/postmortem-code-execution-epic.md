# Postmortem: the code-execution epic (`genetics-results-suite-4h6`)

Written 2026-08-31, immediately after the epic closed. Sources: the epic's own beads
database (373 issues, 334 closed), `git log master..staging` (226 commits), and the three
session transcripts (`~/session.txt`, `~/session2.txt`, `~/session_daly.txt`).

Scope: **why it took as long as it did.** Codebase and doc cleanup is a separate exercise;
this file only names structural causes and process changes.

---

## The one-sentence answer

The work was not unusually hard and the agents were not unusually bad. **Nothing in the
setup was able to say "enough."** The one measurement that could have ended the epic was
built on day one and never run; the one environment that could have tested it was
forbidden until the epic was "done"; and the backlog was a generator rather than a queue.
With no measurable finish line, the only remaining stop condition was exhaustion of a list
that grew as fast as it was consumed.

Measured shape of that: **25 epic children were planned on 2026-08-07; 99 existed by the
end.** Alongside them, **132 non-epic beads were filed during the epic** — more new work
discovered than the epic originally contained.

---

## Findings

### 1. The kill criterion was built and never fired

`4h6.4` (paired A/B replay benchmark harness) was created 2026-08-07 and finished
2026-08-28. `4h6.23` — *run it and decide the rollout on the numbers* — was descoped by
the user on 2026-08-30, one day before close.

So the epic's entire economic premise (median context 39k → 117k tokens across a turn,
uncached share 11% → 52%, the 13.5% of turns that burn 35% of spend) was **never measured
against the thing that was built**. The epic's own honest read admits the median turn is 2
roundtrips — a ceiling of 2 → 1.

The conservative branch was the status quo, so the close is legitimate. But the *sequencing*
is the failure: a gate that runs last cannot stop anything. It ran three weeks of hardening
before it could have said "this is not worth a 6,500-line supervisor."

> **Rule:** the measurement that could kill the epic runs *before* the thing it would kill,
> on whatever crude version exists. A gate at the end is a report, not a gate.

### 2. Live verification was deliberately gated to the end, so risk was retired last

`zg6` (2026-08-14) forbade any live-cluster mutation "until the epic is done and tested
locally." The instruction was right on its own terms — there was one cluster and it was
production. The consequences were not:

- The two beads that could *prove* the design — `4h6.26` (does the NetworkPolicy actually
  hold?) and `4h6.51` (does the supervisor survive gVisor and a read-only rootfs?) — sat
  blocked from 2026-08-14 and ran on **2026-08-31**, the last day.
- On 2026-08-27, twenty days in, it emerged that *the deploy target was not reachable from
  the machine doing the work at all*. Closing the gate meant a machine handoff, not a next
  task. Nothing in the plan had encoded that.
- When a real staging environment finally existed (daly-staging, brought up 2026-08-26),
  **the epic closed in five days**, and the deploy window immediately produced findings no
  amount of reading had: `bk6` (`/scratch` appears to be a gVisor-internal tmpfs, so the
  512Mi `sizeLimit` and the ephemeral-storage cap may not bind at all — a documented control
  that may be inert).

In the meantime the loop substituted *analysis* for *evidence*. That is the direct cause of
the pattern named repeatedly in the transcripts: "five of seven cycles found a stale or
false rationale rather than broken behaviour." When you cannot run the thing, all you can
audit is the prose about it.

> **Rule:** build the cheap disposable environment first, as its own task, before the
> feature. `44g` did exactly this for BigQuery on 2026-08-13 and it worked. The same move
> was available for the cluster and was made twelve days late.

### 3. One unexamined architecture decision generated roughly a quarter of the backlog

The sandbox runs **many executions inside one long-lived container, forked from one process,
under one shared uid**. That decision was never a bead. It arrived implicitly with the
supervisor batch (`4h6.38`–`.52`), all fifteen of which were created on 2026-08-14 and none
of which existed in the 2026-08-07 plan.

`4h6.55` — *"ARCHITECTURE: fork-without-exec plus one shared uid leaves no isolation between
executions"*, filed P0 — was raised on 2026-08-17, **after** the supervisor was built. Its
own close reason lists seven defects caught in review, none by tests, including a control
socket that desynchronised such that the supervisor would "killpg and reap the previous
user's child while its own ran unwatched."

Everything downstream is the same root: `.62` (setsid escapee holds a drain thread for the
pod's life), `.66` and `.68` (process-group and subreaper kill gaps), `.82` (same-uid
artifact planting), `.83` (a resident reads the next execution's token file), `.88`
(per-execution artifact encryption, ~3 days), `.89` (FIFO planted under an artifact name),
`.91` (shorten the retention window). A per-execution container or per-execution uid deletes
that class outright.

The loop is excellent at hardening a design and structurally incapable of questioning one:
beads are tasks, and an architecture is not a task. Nothing in `bd ready` will ever say
"the isolation unit is wrong."

> **Rule:** before decomposing an epic into beads, write down and review the two or three
> decisions the decomposition assumes. For this epic there was exactly one that mattered:
> *what is the isolation unit — the container, the process, or the uid?* Ten minutes of that
> question was worth about twenty-five beads.

### 4. The orchestrator's rulebook was written during the run, at full price

`~/.claude/skills/beads-ralph-wiggum/SKILL.md` went from 311 lines to 572. Every control
that governs cost and termination was added on **2026-08-27** — COST DISCIPLINE, THE
FRONTIER (the three-outcome filing gate), the convergence hard stop, MODEL AND EFFORT
ROUTING, RULING FROM THIS SEAT (the probe rule), VALIDATION TIERS, TRUSTING THE TEST SIGNAL,
SHARED-TREE RULES, CROSS-REPO BEADS RESOLUTION. The repo's own "a finding is not
automatically work" section landed 2026-08-19, day 13 of 25.

So roughly the first two-thirds of the epic ran with **no frontier test, no probe rule, no
budget block, no convergence stop, and no validation tiers**. The rules are good and they
were paid for in real bugs — but they were paid for by this epic, and their absence is a
large part of why this epic looks the way it does.

Two live defects in that rulebook, worth fixing before the next run:

- The skill and this repo's `CLAUDE.md` both direct the reader to *"the filing gate in the
  global CLAUDE.md."* That file existed only on the finngen machine; it was absent from this
  machine for the entire daly-staging phase and was copied across on 2026-08-31, after the
  epic closed. So the rule that is supposed to stop backlog growth was unreadable on the
  machine doing the work. The deeper problem is that the global `CLAUDE.md` and the
  orchestrator skill are unversioned per-machine files that had already silently diverged —
  they belong in a repo, like everything else that governs the work.
- That global `CLAUDE.md` in turn makes architecture exploration a mandatory, "NEVER skip"
  step of the Feature Planning Workflow, and routes it to an `architecture-explorer` agent
  that was **not installed on this machine** — no `~/.claude/agents/` directory existed at
  all until it was added on 2026-08-31, after the epic closed. Compounding it,
  `genetics-mcp-server/docs/project-spec.md:3011` cited the agent at a project-relative path
  (`.claude/agents/architecture-explorer.md`) that exists in none of the five repos, so the
  one written pointer to it was also wrong; corrected to the user-level path on 2026-08-31.
  This is very likely the proximate cause of finding 3: the step that would have asked "what
  is the isolation unit?" could not run — and, worse, **nothing reported that it had not
  run**. Silent absence is the defect; the missing file was only its occasion.
- The skill contains two rules pulling opposite ways — KEEP RUNNING ("do not end the turn
  on the report") versus the convergence hard stop — with no ordering between them. The
  transcripts show the user having to type "Continue", "Continue and then continue. Don't
  stop" while the backlog was simultaneously growing. Both symptoms at once means neither
  rule was governing.

> **Rule:** freeze the process rules before starting an epic. If a rule has to change
> mid-epic, treat that as a signal about the epic's scope, not just a skill edit.

### 5. Verification outgrew implementation, with no rule for when hardening becomes redesign

Across both logs, ~108 agent dispatches: roughly **23 implementation, 36 review/blind
validation, 34 fix, 11 probe**. Two thirds were checking or repairing rather than building.
(Keyword classification of dispatch titles, so approximate.)

That ratio is partly *earned*. Blind validation repeatedly found what nothing else did — the
transcripts record it catching a fix that would have converted a truncated response into a
process that never exits, and two recycled-PID kills of unrelated processes. This is not
waste and should not be cut.

What was missing is a **stopping rule for the hardening spiral**. The clearest instance is
`b1r` (make `rollout.sh` refuse a cluster the deployment does not name): seven agents, three
blind rounds, +369 lines, each round finding a genuinely new class of silent fall-through to
production. The orchestrator itself wrote "well past where I'd normally stop." The user
ended it in five words — *"make kube_context mandatory instead"* — which deleted the entire
parsing problem rather than hardening it a fourth time.

The signal was available at round two: **three consecutive blind rounds each finding a new
class is evidence about the design, not about the implementation.** The skill's existing
"stop after 2 fix rounds" applies to findings on one task; it needs a sibling rule for
"stop after 2 rounds that each find a *new class*" — and that one escalates to the user as a
design question rather than dispatching another fix.

### 6. The prose surface is the largest rot source, and nothing checks it

- `docs/code-execution-security.md`: **6,213 lines**
- `docs/project-spec.md`: 2,784 · `docs/chat-tool-reference.md`: 2,298
- `sandbox/supervisor.py`: 6,512 lines, of which **1,228 are comment-only** (19%), plus
  docstrings
- The epic added **11,825 lines of docs** against ~9,500 lines of sandbox code

Every enumeration in that prose is a claim no test verifies, and there are thousands of
them. "Five of seven cycles found a stale rationale rather than broken behaviour" is the
arithmetic consequence, not bad luck. The transcripts name the mechanism precisely for
`chat-tool-reference.md`: it "restates in prose facts that are derived from tool resolution,
so every profile change silently falsifies several sentences at once."

`check-doc-drift.sh` cannot catch this and `8vn` says so explicitly: it warns on *path
pairs*, while every drift actually found was an enumeration **inside a file both sides of
the pair already touched**. The doc-ownership table is a real control for a class of error
that has not yet occurred, and no control at all for the class that occurs every cycle.

The durable answer is the one the transcript reached: **derive the tables rather than
re-deriving them by hand each cycle**. The epic already proved this works — the
generated-not-transcribed schema/stub contract with its `PLACEHOLDER` build gate is the one
documentation surface that did *not* rot, because a build check fails when it is stale.

### 7. Environment friction was self-inflicted, and about 7% of the backlog

Seventeen epic-era beads are pure tooling friction: `0xs` (bd exports to the main checkout,
so every beads commit from a worktree is a silent no-op), `e47` (sync-datasets skips
silently from a worktree), `xwf` (agents dispatched into a sibling repo cannot read the
beads database), `6o3` (worktree venv trap: `uv run` resolves to the pyenv shim and tests
the main checkout), `82s` (terraform from a worktree proposes destroying both log sinks),
`8wh`, `rxw`, `dqa`, `e96`, and others.

The common cause: **a git worktree + five sibling repos + a beads store that resolves by
cwd**. Several tools failed *silently* in that layout, which is the expensive kind. Add two
machines with different cloud estates and a whole class of "which cluster am I on" work
appears — three of the last week's beads (`b1r`, `mrg`, `4h6.93`) are exactly that.

### 8. The commit count is mostly bookkeeping — don't read it as churn

**96 of 226 commits (42%) are beads bookkeeping**, because the skill's Step 6 mandates a
separate `.beads/issues.jsonl` commit after each close. That leaves ~130 code commits for
90 closed beads, which is normal. "Hundreds of commits" overstates the churn considerably;
the duration problem is real, the thrash problem is smaller than it looks.

---

## What actually went right, and should not be traded away

- **The build gate beat the test suite.** Eleven in-image checks caught the pydantic closure
  regression and surfaced `tbg`; no unit test saw either. Generated-and-checked beats
  written-and-trusted, every time.
- **Blind validation earned its cost.** It found things sighted review did not, repeatedly,
  including two production-mutating paths in `b1r` and three defects in the supervisor
  batches. Keep it; budget it.
- **Negative controls.** Ten defects that can be restored on demand to prove their tests
  still bite. This is why "the suite is green" started meaning something.
- **Close reasons as handoffs.** The epic's close reason is a genuinely usable document a
  year from now. That discipline is why this postmortem could be written from the tracker
  rather than from memory.
- **The user's interventions were consistently the highest-leverage events in the log** —
  "make kube_context mandatory instead", "leave 5p5 parked", the `4h6.23` descope. Each
  collapsed work the loop would have continued. That is an argument for *escalating sooner*,
  not for escalating less.

---

## Changes to make before the next epic

1. **Run the kill measurement first.** If an epic has an economic justification, measure it
   against a crude prototype in week one. No gate at the end.
2. **Stand up the disposable test target as task #1.** Never gate the only feedback loop
   behind "when everything else is done." If the target is on another machine, that is a
   scheduling constraint that goes in the plan on day one, not a discovery on day twenty.
3. **Review the architecture assumptions before decomposition.** Write the two or three
   decisions the bead split assumes, and have them challenged once, deliberately. A bead
   list cannot question its own premise.
4. **Add a design-escalation rule to the skill:** two validation rounds that each find a
   *new class* of defect on the same component stop the loop and go to the user as a design
   question. (Distinct from the existing "2 fix rounds on one finding" rule.)
5. **Done 2026-08-31:** the global `CLAUDE.md` and the `architecture-explorer` agent are now
   present on this machine, and the global file has been revised — the kill criterion and the
   proving ground are now numbered steps 2 and 6 of the Feature Planning Workflow, a missing
   exploration agent must halt the workflow rather than be absorbed, and the design-escalation
   rule from item 4 is in place. `genetics-mcp-server/docs/project-spec.md:3011` now points at
   `~/.claude/agents/architecture-explorer.md`. Still open: these files are unversioned
   per-machine copies and should live in a repo.
6. **Resolve KEEP RUNNING vs the convergence stop.** State which wins and when; the epic
   showed both failure modes simultaneously.
7. **Derive documentation that enumerates.** Anything that lists views, tools, endpoints,
   env vars, profiles or workloads should be generated with a build gate, following the
   schema/stub pattern that already works. Prose that restates a computed fact is a defect
   with a delay fuse.
8. **Work in the primary checkout, not a worktree,** for multi-repo epics — or make the
   worktree preflight a hard failure rather than a warning. Silent no-ops cost more than the
   isolation bought.
9. **Time-box the premise.** An epic that has not demonstrated its own justification within
   about five working days gets re-scoped by the user, not continued by the loop.

---

## Overall read on the codebase

The engineering that landed is good, and the epic's own summary is accurate rather than
generous: gVisor on a tainted pool, uid 65532, read-only rootfs, no GCP identity, one mount,
a generated schema/stub contract with a build gate, verified live by digest comparison.

Two structural things stand out for the cleanup pass, stated without recommendations:

- **`sandbox/supervisor.py` (6,512 lines) and `scripts/test-supervisor.py` (6,449 lines) are
  each single files.** Almost every supervisor bead in the epic touched both, which
  serialised the work, made concurrent tasks collide, and made each review expensive.
- **The documentation is now larger than the code it describes and is maintained by hand.**
  `docs/code-execution-security.md` alone is 6,213 lines. It is the single largest source of
  the "the code was right, the explanation wasn't" finding class, and the only reliable fix
  is to generate the parts that enumerate.
