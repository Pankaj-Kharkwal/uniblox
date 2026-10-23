Your job right now: fix the module of ONE does block so that its failing tests pass.

The tests were derived from the block's examples and rules. They are the contract and they
cannot be changed: only the module can. You get the block, the current module, and the pytest
output of the failing tests.

Requirements, all mandatory:

1. Keep the function's name and signature exactly as they are. Tests call it that way.
2. Keep every rule guard (`guard(..., "<guard id>")`) and every requirement from the original
   instructions: imports, the `now` parameter, sessions, the outbox.
3. Change only what is needed to make the failing tests pass without breaking the passing
   ones. Do not special-case test values; implement the behavior the block describes.
4. If a test seems to contradict the block, follow the test where it reflects an example or
   rule, and say so in `notes`.

Return the complete corrected module in `module_source` (no markdown fences) and, in `notes`,
one or two sentences on what was wrong.
