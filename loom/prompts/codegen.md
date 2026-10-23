Your job right now: write the Python module for ONE does block.

Requirements, all mandatory:

1. Define exactly one public, top-level function, with the exact name you are given, in a
   module of its own. Put helpers in the same module with a leading underscore. Use full type
   hints and add a short docstring that states the behavior and the decisions it honors.
2. Import the project's own code with absolute imports from the package you are given
   (`from <package>.models import ...`). Never redefine the models; they are generated from
   the things and already exist. Use only the allowed imports.
3. Time: if the behavior depends on the current time or date, take a keyword-only parameter
   `now: datetime | None = None` and start with `now = now or utcnow()` (from
   `<package>.runtime`). Never call `datetime.now()`, `date.today()` or `time.time()`
   yourself. `now` is timezone-aware; when the spec or a decision names a time zone, convert
   with `zoneinfo.ZoneInfo` before taking dates.
4. Database targets: take the SQLModel `Session` as the first positional parameter named
   `session`, query with `sqlmodel.select`, modify the objects you load, `session.add` them,
   and finish with `session.flush()`. Never commit, never create engines or sessions.
   Datetimes read back from the database are timezone-aware (UTC).
5. Emails are never sent. "Queue an email" means `queue_email(...)` from `<package>.outbox`.
6. Rules: every never/always rule has a guard id such as `"chase_overdue_invoices:R1"`. Call
   `guard(<condition that must be true>, "<guard id>", <optional detail>)` from
   `<package>.runtime` for every rule. Two situations:
   - If a rule forbids an input the caller might pass (an example expects "a rule violation"),
     the guard is what rejects it: check it before changing anything.
   - Otherwise write the logic so the rule always holds (skip, filter, cap...), and still put
     the guard immediately before the action the rule protects, so that a bug raises
     `RuleViolation` instead of silently breaking the rule.
7. `# decided:` lines are hard constraints, exactly like rules. `~` soft preferences are
   guidance: interpret them sensibly, they are not tested literally.
8. Examples are the contract. Tests will be written from the examples and rules using only
   your function's signature (not its body), by someone who sees the same models, runtime and
   fixtures as you. So choose parameters and a return value that make each example natural to
   set up and check, and return something useful (the updated objects, or a small result
   object) rather than None.
9. `exact` helpers are verbatim author code: import and call them, never copy or change them.
10. If the block has an `every ...` trigger, fill `schedule` with a 5-field crontab in
    APScheduler's `CronTrigger.from_crontab` syntax (minute hour day month day_of_week, with
    day names like `mon-fri`) and an IANA time zone, honoring any decisions. Otherwise leave
    both fields as empty strings.
11. The module must stand on its own: no mention of the compiler or of these instructions.

Return the complete module source in `module_source` (no markdown fences), and in `notes` one
or two sentences on any interpretation you made.
