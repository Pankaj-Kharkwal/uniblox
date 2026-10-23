Your job right now: translate the examples of ONE does block into a pytest module.

You see the function's signature but not its body. Test the behavior the examples state, not
an implementation.

Requirements, all mandatory:

1. Write one test function per example, named `test_e<n>_<few_words>` (for example
   `test_e1_overdue_invoice_gets_first_reminder`). Each test calls the function under test.
2. Every `expect` clause is numbered (E1.1, E1.2, ...). Each clause needs at least one check
   in that example's test: an `assert` statement, or `pytest.raises(RuleViolation)` (from
   `<package>.runtime`) when the clause expects a rule violation. Report every check in
   `tests[].clauses` as `{"clause": "E1.2", "check": "<the statement>"}`, where the
   statement is copied exactly, on one line, from your module: the `assert ...` line, or the
   `pytest.raises(...)` call.
3. Do not assert anything the example does not state, beyond what is needed to observe it.
4. Use the fixtures from `tests/conftest.py` as parameters: `now` (a fixed, timezone-aware
   datetime) and, for database targets, `session`. Express relative times in a `given`
   ("3 days ago", "2 hours ago", "tomorrow") relative to `now`; for dates, use the time zone
   the spec or its decisions name, otherwise UTC. Always pass `now=now` to functions that
   take it.
5. Build inputs with the generated models. Fill required fields the example does not mention
   with plausible, fixed values. For database targets, add the objects to `session` and
   flush before calling the function; to observe changes, read the objects again (or
   `session.refresh` them) after the call.
6. Observe queued emails with `queued_emails(...)` from `<package>.outbox`.
7. No randomness, no sleeping, no network, no files, no mocking of the function under test.
   Absolute imports only. The module must not mention the compiler or these instructions.

Return the complete module in `module_source` (no markdown fences) and the clause mapping in
`tests`.
