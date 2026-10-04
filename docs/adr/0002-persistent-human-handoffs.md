# 0002 — Persistent handoffs and deterministic digital routing

Status: accepted, 2026-10-03. Policy choices explicitly confirmed by the user.

The LLM supplies bounded triage; the backend selects agents, owns state transitions
and attaches existing committed simulator evidence. Persist cases in `simulator`,
separate model context from verified facts, and preserve `/me/handoff` as action
history. HTTP and internal tools share `HandoffStore`; no generic execution route.

Customer and agent sessions use distinct tables. Only provisioned Active snapshot
Digital/Hybrid agents are candidates. Exact specialty is required when requested;
null is allowed only for nonspecialized cases. Fraud requires Fraudes and at least
high severity, complaints Quejas y Reclamos, technical support Soporte Técnico.

Use explicit experience floors and deterministic lexicographic ranking; Premium
is a soft simulated preference. Critical requires Specialist by default. Senior
fallback must be explicitly enabled by reason in deployment configuration and
cannot override a triage minimum of Specialist. Unmatched critical cases remain
unassigned in critical_review. Customers can cancel only before acceptance.

Monthly snapshots cannot support live availability or live-load claims. Reject
invented ranking weights and automatic low-experience fallback. Persist routing
provenance and expose aggregate case counts without interpreting assignment as
resolution. A dedicated inherited read-only column-grant role accommodates the
current ETL privilege reset without changing ETL source or granting contact data.

Consequences: minimal deployment requires grants, provisioned agent accounts and
polling. No live workforce system, human dashboard, external service or LLM provider.
See ../human-handoffs.md for executable contracts and limitations.
