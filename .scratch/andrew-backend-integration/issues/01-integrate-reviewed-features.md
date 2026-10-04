# 01: Integrate useful backend additions with current main

**Qué se construye:** Preserve Andrew's four original feature commits while making
tools, confirmation, handoffs and metrics available with the accepted main fixes;
close confirmed authentication/recovery/readiness integration failures.

**Bloqueado por:** Nada (se puede empezar ya). Remote candidate `142608b` and main
`48f26fc` freshly inspected; no newer recovery branch or open PR existed.

**Estado:** cerrado

The user explicitly authorized integration, corrective implementation, draft PR,
security/code review and merge. Earlier R1 reservation was checked against current
remote work before implementation; recovery here is attributed to this integration,
not to Andrew. The separate historical docs follow-up is outside this issue.

- [x] Preserve both current main and Andrew's original commit history.
- [x] Retain reviewed credential rotation/timing/pagination and receipt-state fixes.
- [x] Reproduce and close agent admission and handoff credential revocation races.
- [x] Recover unavailable assigned/accepted agents with deterministic routing and audit.
- [x] Reject unsafe/terminal recovery and retain original evidence/lifecycle history.
- [x] Readiness detects missing handoff source-column privileges.
- [x] Full `make check`: 298 passed, zero skips (30.85s); restricted startup/process/database restart.

Final-head security/code review and required GitHub checks are separate merge gates
tracked on the integration PR; this issue closes the locally verified implementation.

No deployment, production data, real payments, frontend/provider implementation,
desktop UI, historical docs publication or teammate branch rewrite is authorized.
