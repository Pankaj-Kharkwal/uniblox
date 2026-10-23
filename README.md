# Loom v0

**Write what you mean. Get code you can trust.**

Loom is an intent-first language for vibe coding. A `.loom` file holds plain-language intent,
hard rules and executable examples. The Loom compiler uses Claude to turn it into a real,
tested, standalone Python project, then **locks** it:

- **Ambiguities are asked, not guessed.** Before writing code, Loom lists what your prose
  leaves open and records each answer in the file as a `# decided:` line.
- **Examples and rules become tests.** Those tests are the contract. Loom repairs code until
  they pass and refuses to edit the tests themselves, checking their hashes to enforce it.
- **Nothing changes unless your intent changes.** A rebuild regenerates only the blocks whose
  meaning changed. An unchanged spec makes zero model calls.

```
app InvoiceChaser
target: python 3.12, fastapi, sqlite

thing Invoice
  client: text
  client_email: email
  amount: money (INR)
  due: date
  status: one of draft, sent, paid, overdue
  reminders_sent: number = 0
  last_reminded_at: datetime?

does chase overdue invoices
  every weekday at 10:00 IST
  find invoices where due < today and status = sent or overdue
  mark them overdue
  queue a reminder email to the client ~ polite first, firmer after 2nd reminder
  increment reminders_sent and set last_reminded_at
  never email the same client twice in 24h
  always skip invoices with status paid

example
  given invoice due 3 days ago, status sent, reminders_sent 0
  expect status overdue, 1 email queued, reminders_sent 1
```

## 60-second quickstart

```bash
git clone https://github.com/Pankaj-Kharkwal/uniblox loom && cd loom
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

# Offline: replays the recorded model responses shipped in tests/fixtures/llm.
LOOM_MOCK=1 loom build examples/todo.loom --yes
#   decided (complete_task): if no task matches, raise LookupError; ...
#   GREEN: 7 passed in out/todo_list
LOOM_MOCK=1 loom build examples/todo.loom --yes
#   nothing to rebuild: todo.loom matches todo.loom.lock

cd out/todo_list && pytest            # the generated project stands on its own
```

With Claude, set a key and drop `LOOM_MOCK`:

```bash
export ANTHROPIC_API_KEY=...          # a Claude Console key
loom build examples/invoice_chaser.loom --yes
```

`--yes` accepts the suggested answer to every ambiguity. Without it, `loom` asks you each
question on the terminal. Either way, the answers are written into the `.loom` file.

Mock mode only replays requests it has seen before. Editing an example file changes the
prompts, so mock mode then stops with a clear message, and you need a key.

## The language

| Block | Purpose | Compiles to |
|---|---|---|
| `app <Name>` + `target:` | Project header and stack (`python 3.12`, optional `fastapi`, `sqlite`) | Project scaffold and config |
| `thing <Name>` | Data shape, with forgiving types | Pydantic model (SQLModel table when a database is targeted). Deterministic, no model call |
| `does <description>` | Behavior as prose steps | One Python function per block, named after the description |
| `never ...` / `always ...` in a `does` | Hard rules | A runtime guard in the function, plus a pytest/Hypothesis test |
| `example` with `given` / `expect` | The contract | pytest cases; every comma-separated `expect` clause must be asserted |

Inline markers:

- **`~ text`** after a step is a soft preference: guidance the model may interpret freely.
- **`exact { ... }`** is raw Python copied verbatim into the project. No model touches it. Put
  the code on the lines after `exact {` and close with `}` lined up with `exact`.
- **`# decided: ...`** is the recorded answer to an ambiguity, written by the compiler and
  treated as a hard constraint. Indent it under the line it answers.
- **`every ...`** inside a `does` block is its schedule.
- Any other **`# comment`** is ignored.

An `example` belongs to the nearest `does` above it; `example for <does description>` points
it elsewhere.

Forgiving types:

| Loom | Python |
|---|---|
| `text` | `str` |
| `email` | `EmailStr` |
| `number` | `int` |
| `money (INR)` | `Decimal`, tagged with its currency |
| `date` / `datetime` | `date` / timezone-aware `datetime` |
| `one of a, b` | a generated `StrEnum` |
| `T?` | `Optional[T]`, default `None` |
| `= value` | a typed default, checked at compile time |

An unknown type is a compile error with a suggestion (`string` → "did you mean 'text'?"). Every
error has a line, a column, the source line, a caret and a hint.

## Commands

```
loom check <file>   parse, type-check, ask about ambiguities; exit 1 if any stay open
                    (--no-scan: parse and types only, no model call)
loom build <file>   full pipeline -> out/<app_name>/ and <file>.lock
loom test <file>    run the generated pytest suite only
loom diff <file>    intent diff against the lock, then which blocks would regenerate
                    (--exit-code: exit 1 if anything would, for CI)
loom eject <file>   strip Loom metadata and leave a plain Python project
```

Flags:

- `--model`: default `claude-sonnet-5-5`, or `$LOOM_MODEL`.
- `--max-iters`: repair rounds, default 4.
- `--yes`: accept the suggested answers.
- `--force`: rebuild everything, overwriting hand edits.
- `--out`: output directory.

## How a build works

1. **Parse** with a Lark LALR grammar plus an indentation postlexer, into a typed AST
   (`loom/nodes.py`). Resolve the forgiving types.
2. **Plan from the lock.** A `does` block is rebuilt when it is new, when its normalized AST
   changed, when the things/app/`exact` it depends on changed, when it was red last time, or
   when its files are missing.
   - Whitespace, re-indenting and comments never count as changes.
   - With nothing to rebuild, no model client is even created.
   - Generated files edited by hand are refused unless you pass `--force`.
3. **Ambiguity scan** (one model call per dirty block). The answers are written back as
   `# decided:` lines. An ambiguity left unanswered is a compile error.
4. **Scaffold** deterministically:
   - models from things;
   - a runtime with `guard()`/`RuleViolation`;
   - an outbox table (emails are queued, never sent);
   - database helpers with timezone-safe datetimes;
   - test fixtures with a fixed `now` and derandomized Hypothesis;
   - `exact` modules;
   - APScheduler and FastAPI glue.
5. **Generate.** One structured-output call per dirty block writes its module. Two more calls
   translate its examples and rules into pytest; the test writer sees the function's
   signature, never its body.
   - Every output is checked structurally: the function exists, each rule has a guard,
     imports come from an allowlist, nothing mentions Loom, and each example test calls the
     function and asserts every `expect` clause.
   - A failed check gets one corrective retry.
6. **Green loop.** The tests are hashed and made read-only, then pytest runs. Only the failing
   blocks are regenerated, from their failure output, for up to `--max-iters` rounds.
   - After every round the test hashes are re-checked; any change aborts the build.
   - A red build ends with a report: failing tests per block, repair attempts, and the note
     that your examples are the contract.
7. **Lock.** `<file>.lock` (JSON) records:
   - the Loom version and the model;
   - for each block: its normalized AST, source hash, input hash, generated files and test
     files with their content hashes, signature, schedule, status, repair rounds and
     timestamps.

The lockfile is the record of what was built. `loom diff` reads it to say, in plain English,
what changed, for example `Added rule: never email the same client twice in 24h`.

### Determinism

Current Claude models reject `temperature`, so Loom doesn't send it. Determinism comes from two
places instead:

- **The lockfile.** Unchanged blocks are never regenerated.
- **A disk cache** keyed by `(model, prompt hash)`, in `~/.cache/loom` or `$LOOM_CACHE_DIR`.
  An identical request is never sent twice.

Prompts are built from a canonical rendering of the AST, so formatting edits never change a
cache key.

## The generated project

```
out/invoice_chaser/
  pyproject.toml                    # pip install -e . && pytest, nothing else needed
  src/invoice_chaser/
    models.py                       # from the things
    runtime.py                      # guard(), RuleViolation, utcnow()
    outbox.py  db.py                # queued emails; sessions, tz-safe datetimes
    chase_overdue_invoices.py       # one module per does block
    scheduler.py  api.py            # APScheduler job, FastAPI /jobs/<block>
  tests/
    conftest.py                     # fixed `now`, in-memory database, Hypothesis profile
    test_chase_overdue_invoices_examples.py   # locked
    test_chase_overdue_invoices_rules.py      # locked
```

The generated project never imports `loom`. `loom eject` removes the header comments that
mention it and deletes the lock. The folder is then an ordinary Python project, and its tests
still pass.

## Working on Loom

```bash
pip install -e '.[dev]'
pytest                         # offline: tests/conftest.py forces LOOM_MOCK=1 (LOOM_LIVE=1 to opt out)
pytest --cov                   # 85% gate on the non-LLM modules
ruff check . && ruff format --check . && mypy
LOOM_RECORD=1 loom build examples/todo.loom --yes   # record real responses as fixtures
```

`tests/` covers:

- parser golden ASTs and malformed inputs with their exact messages;
- the type table and hash stability;
- the lockfile round trip and incremental rebuilds (one edited block regenerates alone);
- the green loop (repair, give up at `--max-iters`, any change to a test aborts);
- full builds of both examples, `diff` and `eject`.

## Known limitations

These are stated honestly:

- **The recorded model responses are hand-written.** This environment had no API key, so
  every file in `tests/fixtures/llm/` is marked `"source": "hand-authored"`. The pipeline
  around them is real; how often Claude itself reaches green is **not yet measured**.
  Re-record with `LOOM_RECORD=1`, then see `docs/PLAN.md` (phase 2).
- **Untested against the live API:**
  - the server-side refusal fallback (a beta; `LOOM_NO_FALLBACK=1` turns it off);
  - prompt-cache hit rates.
- **A generated test can be wrong.** The loop never edits tests, so a mistranslated example
  stays red. Fix the example, then rebuild.
- **Generated code runs on your machine.** The green loop runs pytest in the current
  interpreter. The import allowlist and the ban on `open`, `exec`, `eval`, `os` and
  `subprocess` reduce risk, but they are not a sandbox.
- **Rebuild granularity is a whole block.** Any change to a `does` block regenerates its code
  and tests, and a change to a thing regenerates every `does` block. If another block depends
  on a changed signature, its tests fail and the loop repairs it.
- **Small type system:** no booleans, lists, relations between things or foreign keys. One
  database (SQLite) and one language (Python 3.12+).
- **Grammar quirks:**
  - a prose line that starts with `every`, `never` or `always` is a keyword;
  - `#` always starts a comment;
  - an `exact` block ends at the first line holding only `}` at its indentation.
- **The scheduler is a stub.** It uses APScheduler's `BlockingScheduler`, with no
  persistence, retries or real email delivery.
- **Single-file projects only.** Windows is untested.

## Roadmap

These are not in v0, in rough order:

- **Real-model benchmark:** 20 specs, hands-off green rate, cost per block.
- **Developer workflow:** `loom build --check` for CI, a GitHub Action, `loom diff` in pull
  requests.
- **For non-programmers:** `loom new "<describe the job>"`, plain-English red reports, and
  `loom run <block>` without pip.
- **More targets:** TypeScript first, then a Rust target, whose compiler would act as a second
  checker in the green loop.
- **Language:** multi-file projects and `import`.
- **Tooling:** a language server and editor plugins, a web UI, deployment.

See `docs/PLAN.md` for the problem statement, the success metrics and the phased plan.

## Repository layout

```
loom/            the compiler (cli, grammar, parser, types, hashing, llm, ambiguity,
                 scaffold, generate, tests_gen, validate, runner, loop, lock, diff, eject)
examples/        invoice_chaser.loom (reference), todo.loom (fast smoke test)
tests/           the compiler's own test suite, plus recorded fixtures
docs/PLAN.md     problem statement, scorecard, phases
legacy/          the archived Uniblox e-commerce backend this repository started as
```
