---
name: unit-test
description: Implement and run pytest regression tests for Python changes.
---

# unit-test

Read the implementation and existing tests. Cover the reported bug, boundaries, and failure paths. Use Decimal for money. Keep tests deterministic; mock external APIs in unit tests. Run python -m pytest and inspect its exit code. Report the command, passed/failed counts, and any untested integration. Never remove assertions or mark failing tests skipped to claim success.

Before implementation, map each explicit requirement to a test. Distinguish zero,
empty and missing values; strict integer inputs must reject bool. For decimal
inputs validate finiteness BEFORE comparison/arithmetic; normalize parsing errors
to the exception promised by the API. Test state after failure and after duplicate
input when relevant. Do not silently invent additional constraints.

After changes use verify_changes when available. It runs a separately drafted
requirements contract and the project tests, and checks changed module imports.
These internal checks can be wrong: report a contradictory contract instead of
deleting or weakening it. External evaluation remains independent.
