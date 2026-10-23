Your job right now: turn the never/always rules of ONE does block into pytest tests.

You see the function's signature but not its body. Each rule must hold for every input the
function accepts, so test it through the public function, as a property where possible.

Requirements, all mandatory:

1. For every rule (R1, R2, ...) write at least one test named `test_r<n>_<few_words>` that
   calls the function under test and asserts the rule. Report each in `tests` as
   `{"rule": "R1", "function": "<test function name>"}`.
2. Prefer property-based tests with Hypothesis when the rule ranges over an input space
   (times, counts, statuses, list contents): use `@given(...)` with small, bounded strategies
   (`st.sampled_from`, `st.integers(min_value=..., max_value=...)`, `st.timedeltas` with
   bounds, short lists). Otherwise write focused scenario tests around the rule's boundary.
3. A rule that forbids an input (for example "never complete a task that is already done")
   is checked with `pytest.raises(RuleViolation)` (from `<package>.runtime`). A rule that
   constrains what the function does (for example "never email a client twice in 24h") is
   checked by asserting on the outcome; it must not raise for valid inputs.
4. The `now` fixture is a fixed, timezone-aware datetime; always pass `now=now` to functions
   that take it. For database targets, tests decorated with `@given` must not use the
   `session` fixture: open a fresh database per example with
   `with memory_session() as session:` (from `<package>.db`). Plain tests may use the
   `session` fixture.
5. Build inputs with the generated models, filling unrelated required fields with plausible
   fixed values. Observe queued emails with `queued_emails(...)` from `<package>.outbox`.
6. No network, no files, no sleeping, no mocking of the function under test. Absolute imports
   only. The module must not mention the compiler or these instructions.

Return the complete module in `module_source` (no markdown fences) and the rule mapping in
`tests`.
