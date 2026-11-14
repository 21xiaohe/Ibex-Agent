---
name: fastapi-endpoint
description: Implement typed FastAPI endpoints with request validation and tests.
---

# fastapi-endpoint

Read the current router and schemas. Define bounded Pydantic input models and typed responses. Reuse authentication and tenant identity; do not trust tenant IDs from request bodies. Keep I/O asynchronous. Release owned resources in finally blocks. Add TestClient coverage for valid requests, invalid inputs, authorization and dependency failures. Run tests and report real results.

Inspect installed dependency APIs before choosing unfamiliar parameters:
inspect_python_api(module="pydantic", symbol="Field") or a sandbox Python
signature check. Do not assume v1 and v2 validators have identical signatures.
Import the changed application before reporting success. Use strict integer
validation when required, and Decimal/string validation for money; do not turn
money into float to simplify a request model. Include missing fields, nonfinite
values, booleans and bounds when required by the user contract.
