Your job right now: find the genuine ambiguities in ONE does block, before any code is written.

A genuine ambiguity is a point where two careful engineers, both reading the block faithfully,
would write code that behaves differently in a way a test or the author would notice. Typical
sources:

- boundaries: `<` versus `<=`, whether a window like "24h" is rolling or per calendar day,
  whether "after the 2nd reminder" means from the 3rd reminder on;
- calendars and time zones: which days a "weekday" covers, which zone "today" is measured in;
- operator precedence in prose: "due < today and status = sent or overdue";
- what a step does to records it does not select, or when nothing matches;
- how two lines interact, for example whether a rule skips a record or fails the whole run.

Not ambiguities (do not report these):

- anything already settled by a `# decided:` line, a rule, a field type, or an example;
- naming, code style, data-structure choice, performance, or implementation strategy;
- the wording of a `~` soft preference, unless it contains a threshold or count that changes
  behavior (then ask about the threshold only);
- requirements the author never hinted at.

Report at most 4 ambiguities, most consequential first, and return an empty list when the block
is clear. For each one give:

- `line`: the number in the `[L<n>]` prefix of the line it concerns, or -1 for the whole block;
- `question`: one short question;
- `options`: 2 to 4 mutually exclusive answers, each written as a complete, self-contained
  decision as it would appear after `# decided:` (for example "weekdays are Monday to Friday in
  Asia/Kolkata time"), each under 120 characters;
- `suggested`: the index in `options` of the answer you recommend: the most conventional reading
  that is consistent with the examples;
- `why`: one sentence on what behavior changes depending on the answer.
