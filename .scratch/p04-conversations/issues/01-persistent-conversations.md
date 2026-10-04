# 01: Persistent authenticated conversations and injected adapter

**Qué se construye:** P04 backend conversation persistence, strict injected proposals,
ES/PT context and correlation with existing tools, confirmations and handoffs.

**Bloqueado por:** P00 accepted cross-repository contract and P01 public ETL fixture
boot are not evidenced in this branch. The user explicitly authorizes implementing
a clean backend seam/stub and documenting dependencies without fabricating acceptance.

**Estado:** cerrado (entrega de ingeniería BCK; gates P00/P01 pendientes)

- [x] Persist authenticated owned conversations and ordered replayable turns/events.
- [x] Validate injected adapter proposals, with explicit deterministic stub mode.
- [x] Keep mutations pending until separate customer confirmation and correlate evidence.
- [x] Correlate handoffs and scope their evidence to the conversation.
- [x] Verify isolation, failures, rollback, concurrency, ES/PT and existing regressions.
- [x] Run backend checks and standards/specification review; record independent-review availability.

Contract and dependency limits: `docs/conversations.md`. This ticket covers backend
engineering delivery only; it does not close the cross-repository P00/P01 gates.

Validation: `UV_NO_EDITABLE=1 make check` passed on Python 3.13.14:
329 tests (52 dedicated P04 cases), Ruff and formatting. `git diff --check` passed.
All installed Python source modules matched the working tree byte-for-byte.
The regular-package install avoids this host's hidden editable `.pth` behavior.

Review: both requested independent review agents were attempted but stopped at the
account usage limit without results. The implementation author reviewed Standards
and Spec separately. Standards: no remaining findings. Spec: fixed invalid calendar
date validation, conversation/handoff language mismatch and adapter cancellation
handling, each with regression coverage; no remaining findings. This is not an
independent review approval. No production data, real ML or integrated ETL acceptance
was exercised.
