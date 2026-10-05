#01: Integrate persistent conversations into the reviewed backend

**Qué se construye:** authenticated ES/PT conversation-to-preparation-to-confirmed
receipt and scoped handoff, retaining accepted security/recovery and team provenance.
**Bloqueado por:** full browser/ETL journey is owned by separate workers; provider ML
implementation stays with the ML team.
**Estado:** en-progreso (implementation/checks complete; external review pending)

- [x] Preserve accepted c7ca7aea and Andrew033dccb/9ad187f histories.
- [x] Keep confirmed authority, publisher pins, cursors, strict grants and operator metrics.
- [x] Derive published card/customer provenance from trusted release manifest.
- [x] Verify actual restricted API/PG restart with ES/PT conversation/receipt/recovery.
- [x] make check443 passed, zero skips; no organizer or production credentials/data.
- [ ] Reconcile final-head code/security review.
- [ ] Parent-coordinated complete synthetic ETL/browser journey and merge decision.
