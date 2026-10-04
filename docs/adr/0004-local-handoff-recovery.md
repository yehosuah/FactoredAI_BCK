# 0004 - Audited local recovery for unavailable handoff agents

**Estado:** aceptada

## Contexto

An accepted case assigned to a disabled/ineligible agent could not resolve,
cancel or use the queued-only reroute operation. Reusing creation or dropping
acceptance metadata would lose the original evidence and lifecycle history.
Recovery must use the existing routing eligibility, without a caller-chosen target.

## Decisión

Add a local administrator `recover` operation for assigned/accepted cases only
when their previous agent fails current routing eligibility. Lock the case,
revalidate account/routing requirements and reuse the deterministic router.
Append the previous lifecycle/assignment and server-derived local/database authority
to `simulator.handoff_recoveries` atomically with reassignment or queueing.
Keep creation provenance and evidence; start a new acceptance cycle. Keep terminal
cases terminal. Do not expose recovery through HTTP or the model tool catalog.

## Alternativas consideradas

- Force queued-only reroute: loses why a previously accepted case was reopened.
- Permit arbitrary customer/agent-selected reassignment: crosses the routing trust boundary.
- Re-enable the original account: ignores source eligibility and revocation intent.

## Consecuencias

Startup migration is additive. Recovery audit history persists across application,
database and ETL source refreshes. The existing database/OS administrator is trusted;
the audit is application-append-only, not an external tamper-proof compliance service.
Human notification, live availability and enterprise authorization remain separate.
