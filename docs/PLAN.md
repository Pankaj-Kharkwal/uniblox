# Loom plan of action

## Problem

People who use AI to write business logic can't trust or maintain what it produces.
Requirements live in chat history, the model silently guesses ambiguities, and every
regeneration can quietly change behavior. So they spend more time re-checking AI code than it
took to generate.

Loom makes the spec the single source of truth. Ambiguities are asked, not guessed. Examples
become tests that can't be edited. Nothing regenerates unless the intent changed.

## Two audiences, one engine

| | Developers / technical founders | Non-programmers (ops, founders, analysts) |
|---|---|---|
| Job | Small jobs that touch money or customers: invoicing, reminders, syncs, pricing rules | The same jobs, without reading Python |
| Needs from Loom | Guarantees: locked code, tests that can't be edited, clean diffs, CI | A plain-English way in and out: draft the spec, explain failures, run the app |
| Front door | CLI + CI (`loom build`, `loom diff`, drift check) | `loom new` from a description, plain-English reports, `loom run` (no pip) |

Both front doors sit on the same engine. That engine is the trust core (phase 1), and it must be
proven against real Claude (phase 2) before either audience gets polish.

## Scorecard

Targets are hypotheses for the pilot.

| Metric | Target | Measured by |
|---|---|---|
| **North star: hands-off green rate**: % of does blocks that reach green within `--max-iters` with no hand edits to generated code | ≥ 80% on a 20-spec benchmark | `scripts/bench.py` (phase 2), lock status |
| Time to first green: quickstart / own spec | ≤ 60 s / ≤ 15 min median | pilot sessions |
| Unchanged rebuild makes zero model calls | always | compiler test suite (CI) |
| Tests edited during the green loop | 0 | hash check in code, test suite |
| Ambiguity questions rated "a real question" | ≥ 70%, ≤ 4 per block | CLI rating (phase 3) |
| Spec edited and rebuilt ≥ 3 times over 2+ weeks | ≥ 40% of pilot users | pilot |
| Cost and time per green block | < $0.25, < 60 s median | lock usage data, bench |
| Non-programmers reaching first green unassisted | ≥ 3 of 5 | pilot |
| Generated project passes `pip install -e . && pytest` on its own | 100% | e2e tests |
| Eject rate, with a reason | tracked, not judged | exit question |

## Phases

### Phase 1: trust core (done: M4–M6 shipped, no API key needed)
- **M4**: green loop (repair only the failing block), test immutability (hash plus read-only),
  lockfile, incremental rebuild, zero-call rebuild.
- **M5**: `invoice_chaser.loom` end to end: outbox, scheduler stub, the 24h rule as a runtime
  guard plus a property test, and a red report when an example is broken.
- **M6**: `loom diff` (intent diff plus code impact), `loom eject`, README with a 60-second
  quickstart and honest limitations.

### Phase 2: reality check (in progress)

Done:
- A Claude Code backend (`--backend claude-code`) runs Loom on a Claude subscription, with no
  API key.
- First real run: both examples built **green with zero repair rounds**. todo took 45 s and
  8 calls (10 tests); invoice_chaser took 41 s and 4 calls (6 tests). The 24h rule's guard and
  property tests caught the same mutations as before.
- Two real-model problems were found and fixed:
  - The ambiguity scan asked about clocks and suggested "server local time". The fix: Loom's
    fixed conventions now go in the shared prompt.
  - A Hypothesis test passed its strategy positionally, so it could not run. The fix: the
    prompt asks for strategies by keyword, and any test that cannot run is re-translated
    before tests are locked.
- Real recordings now back the acceptance tests (`tests/fixtures/llm`). The hand-written
  outputs moved to `tests/fixtures/synthetic`, for the mechanics tests.

Remaining:
- `bench/`: 20 small real-world specs, including `store.loom`, a port of the legacy Uniblox
  store's nth-order discount (the rule that was 3 in one file and 5 in another).
- `scripts/bench.py` reports hands-off green rate, repair rounds, cost and time per block.
- **Gate:** below 60% hands-off green, fix prompts and the loop before any audience work.

### Phase 3: two front doors
- **Developers:** `loom build --check` for CI (fails on drift or a stale lock), a GitHub Action
  example, and docs on reviewing `loom diff` in pull requests.
- **Non-programmers:**
  - `loom new "<describe the job>"` drafts a .loom file and asks its questions.
  - Plain-English red reports: "Example 2 expected 0 emails; the code queued 1."
  - `loom run <block>` runs a job without knowing pip or pytest.
  - A local web page to edit the spec and see green/red (only if the pilot asks for it).
- **Both:** a one-keystroke "was this a real question?" rating on ambiguity prompts, and local
  build stats in the lock (`loom stats`).

### Phase 4: pilot (4 weeks)
- 15 users: 10 developers, 5 non-programmers, each bringing one real job.
- Weekly scorecard. Exit interviews for anyone who ejects or stops rebuilding.

## Risks

1. **Real Claude may not reach green often enough on prose plus examples.** Everything so far
   uses hand-written model responses. Phase 2 tests this first.
2. **Translated tests can be wrong,** and the loop must never edit them. A wrong test means a
   red build, so the red report has to make "the test may be wrong" obvious and fixable from
   the .loom.
3. **Non-programmers may still hit Python.** Install, errors, running the app. Phase 3 measures
   this rather than assuming it away.
