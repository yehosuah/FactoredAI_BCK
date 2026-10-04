# Persistent human handoffs

## Architecture and trust

The future LLM supplies **what help is needed** as bounded triage. The backend
validates it and decides **who may receive the case**. There is no LLM provider,
public tool execution route, external ticket system or automatic bank operation.

`handoff.py` holds typed triage and a deterministic pure routing function.
`HandoffStore` owns authenticated creation, database transactions, evidence capture,
case access, transitions, queue retry and aggregate metrics. HTTP routes and the
controlled tool dispatcher share this implementation. `AgentAuth` resolves a
separate simulator identity; customer tokens do not authenticate agents and vice
versa. All interfaces are synchronous; an async orchestrator must use a threadpool.

`Store.initialize()` creates four additive tables under its existing advisory
startup lock: `simulator.agent_users`, `agent_sessions`, `agent_login_attempts`,
and `handoffs`. No ETL tables are changed. Credentials use existing salted scrypt,
sessions are opaque with SHA-256 hashes at rest and existing session TTL. Login
throttling uses the same 10 attempts / 5 minutes / username-and-peer pattern in a
separate table. Agent session use rechecks enabled account, accepted-release
membership, Active snapshot status and Digital/Hybrid type. These are simulator
credentials, not enterprise IAM or proof of real employment.

`handoffs` stores customer scope and creating username from authentication,
canonical triage, original source release, assignment, lifecycle timestamps,
routing explanation and separate context/evidence JSON. Customer/agent responses
omit customer ID, login identity, idempotency payload and secrets. Agent IDs in
responses identify persisted assignment; they are never accepted as triage.

`model_context` is explicitly **untrusted** and holds only summary, context and
questions. Caller claims, including a claimed refund/block, never become evidence.
`verified_evidence` captures up to 20 most recent committed simulator receipts for
this customer (and selected card, if supplied), plus the selected owned card's
simulator state and source kind. Receipts retain their original release and commit
time; truncation is explicit. This is an immutable creation snapshot, not a promise
of current card state. The original `GET /me/handoff` remains action history.
No handoff is inserted into `simulator.actions` and no banking logic is duplicated.

Do not send credentials or raw chats as context. Unknown arbitrary secrets inside
free text cannot be reliably recognized. The creation interface rejects its current
session token if copied into any input; it never stores an authentication token or
session hash as metadata. Application errors and tool errors omit inputs and SQL
messages. Use TLS and disabled/redacted access logs at deployment, including proxies.

## Contract

`POST /me/handoffs` takes the triage object below and `Idempotency-Key` header
(1–100 characters, `[A-Za-z0-9_.:-]`). All fields are strict; undeclared fields are
rejected at every object level. In particular, no customer ID, agent ID, verified
facts, queue priority or lifecycle state can be supplied.

```json
{
  "reason": "card_support",
  "severity": "low",
  "required_specialty": null,
  "minimum_experience": "Junior",
  "language": "es",
  "summary": "Customer needs help with their card",
  "context": "",
  "unresolved_questions": [],
  "product_id": null
}
```

| Field | Contract |
| --- | --- |
| reason | `fraud`, `complaint`, `technical_support`, `card_activation`, `replacement`, `card_support`, `other` |
| severity | `low`, `medium`, `high`, `critical` |
| required_specialty | Required explicit null, or an exact observed specialty listed below |
| minimum_experience | `Junior`, `Mid-Senior`, `Senior`, `Specialist` |
| language | `es`, `en`, `pt`; mapped to exact comma-delimited snapshot tokens |
| summary | Required nonblank string, 1–2000 characters |
| context | Optional string, at most 2000 characters; default empty |
| unresolved_questions | At most 10 nonblank strings, each 1–500 characters |
| product_id | Optional owned card ID, 1–100 characters; validated through existing Store |

Specialties: `Fraudes`, `Retención`, `Cobranza`, `Soporte Técnico`, `Créditos`,
`Inversiones`, `Ventas`, `Quejas y Reclamos`. Unsupported values return 422, rather
than silently becoming general support. Fraud requires `Fraudes`, complaints require
`Quejas y Reclamos`, technical support requires `Soporte Técnico`. Contradictory or
null specialty in those cases is rejected. Fraud severity is raised to at least high;
submitted severity remains visible in `triage`, effective severity at the top level.

HTTP success returns the persisted resource with `persisted=true`, lifecycle status,
assignment status, nullable assigned agent, queue, manual-routing flag, timestamps,
triage, effective severity/required level, simulated service priority, routing metadata,
model context, verified evidence and limitations. There is no `transfer_succeeded`
claim. A committed unassigned case is a successful registration, not a human transfer.

| Customer route | Behavior |
| --- | --- |
| `POST /me/handoffs` | Validate and create/replay an owned case |
| `GET /me/handoffs` | Own cases; `limit=20` (1–100), `offset=0` (0–10000), `next_offset` |
| `GET /me/handoffs/{handoff_id}` | Own case only; ID is 32 lowercase hexadecimal characters |
| `POST /me/handoffs/{handoff_id}/cancel` | Own queued/assigned case only; empty/no body |

| Agent route | Behavior |
| --- | --- |
| `POST /agent/auth/login` | Username/password, same bounds as customer login; returns agent token |
| `POST /agent/auth/logout` | Revoke agent token |
| `GET /agent/handoffs` | Only cases assigned to this agent; same pagination |
| `GET /agent/handoffs/{handoff_id}` | Assigned case only |
| `POST /agent/handoffs/{handoff_id}/accept` | Assigned → accepted; empty/no body |
| `POST /agent/handoffs/{handoff_id}/resolve` | Accepted → resolved; empty/no body |

Lifecycle: creation → queued or assigned; administrative deterministic retry can
move queued → assigned; assigned → accepted → resolved. Customer cancellation is
allowed only queued/assigned → cancelled. Resolved/cancelled are terminal. A repeated
transition to its current target is idempotent; other invalid transitions return
409. Row locks serialize competing acceptance/cancellation. A resolution records
an assigned simulator agent's acknowledgement, not proof of refund or safe resolution.
Wrong-owner reads/transitions return 404. Missing/expired/wrong-kind sessions return
401; invalid input 422; idempotency conflicts 409; login limit 429; unexpected storage
failure a sanitized 500 (or 503 for unavailable source contract). No failure returns
an assigned/persisted success object. Responses use the existing request-ID error envelope.

## Routing policy v1

Source is the current accepted PostgreSQL release, never sibling files at runtime.
Candidates must have an **enabled provisioned simulator agent account**, Active
snapshot status, type Digital/Hybrid, compatible language, and matching specialty
when required. Null specialty qualifies only when triage explicitly requires none.
No-specialty general cases may also use agents with a known specialty. Unknown
experience levels fail eligibility. No names, contact details, balances, occupation,
branch, shifts or monthly activity enter ranking.

Experience order is explicit: Junior < Mid-Senior < Senior < Specialist. Floors:
low Junior, medium Mid-Senior, high Senior, critical Specialist. Effective minimum
is the higher of severity floor and submitted minimum. Segment never reduces it.

Premium, read only from the authenticated customer's `bank.customers.segment`,
gets a **simulated soft uplift**: prefer the next experience level, capped at
Specialist, if available. Otherwise retain safe-floor candidates. Other/missing
segments get no uplift. Severity always outranks Premium in case lists: severity
descending, Premium first within severity, oldest creation, handoff ID. Pagination
is bounded offset pagination; concurrent lifecycle/source changes are not a frozen
queue view. There is no live queue scheduler or load balancing.

Among candidates meeting the preferred level (or safe candidates if none do), rank
by closest suitable experience, CSAT descending with nulls last, then agent ID
ascending. No numerical weighting and no historical-activity proxy for live load.

Critical first requires Specialist. If none, Senior may be used **only** when the
reason is explicitly enabled in `BCK_CRITICAL_SENIOR_FALLBACK_REASONS` and triage
minimum is at most Senior. Language/specialty/type/status/account filters still
apply. Metadata records policy version, configuration, original Specialist floor,
selected Senior level and `fallback_used=true`. Otherwise persist queued/unassigned
in `critical_review`, `manual_routing_required=true`. Other unmatched cases use
`manual_review`. Basic/Student severe cases retain the same floors.

The monthly snapshot is not presence, shift is not online status, and interactions
are not live workload. Assignment does not mean notified, accepted or resolved.
Agent polling is the delivery mechanism; there are no push notifications.

## Transactions, replays and metrics

Creation uses the same per-customer transaction advisory lock as card actions.
It checks the unique `(customer_id, idempotency_key)` before source reads/routing,
pins a release ID, captures evidence, chooses a candidate and inserts one row in
one transaction. There is no partially committed assignment. Concurrent identical
requests return the same case. Changed triage with the same key returns 409.
Handoff keys have their own namespace, separate from card action keys. Replay
returns the case's **current** lifecycle state, without rerouting or replacing
original evidence. If commit outcome is unknown, retry the same key and payload.

Administrative `reroute` works only on queued cases, applies current accepted
source/policy and cannot receive a chosen agent ID. It updates routing metadata
and assignment atomically; original creation release and evidence remain unchanged.
`routing.release_id` identifies the retry's source. There is no automatic rerouting
of accepted/assigned work if an agent later becomes inactive; operator workflow for
reassignment and human review remains a limitation, not an unsafe automatic fallback.

`GET /operations/metrics` adds `handoffs`, available to existing customer sessions.
It includes service-wide `total_handoffs`, `assigned`, `unassigned`, `by_severity`,
`by_reason`, `by_required_level`, `fallback_assignments`, `critical_review`.
Counts span retained persisted cases; assigned/unassigned refers to assignment
identity even for terminal cases. Critical-review counts only currently queued cases.
Only fixed enum labels and counts are returned; no IDs, text or credentials.
Aggregation failure returns `status=unavailable`, not invented zeros. HTTP success,
assignment and agent resolution must not be used as interchangeable outcome metrics.

## Deployment

1. Deploy the backend against an accepted `card-support-etl-v1` database, with its
   own `simulator` schema as in the existing setup. Back up persisted simulator data.
2. A privileged database administrator runs [handoff-read-grants.sql](../deploy/handoff-read-grants.sql)
   after ETL tables exist. It grants only required columns through a dedicated
   NOLOGIN role inherited by `backend_api`. The current ETL refresh revokes direct
   grants to `backend_api`; independent inherited grants survive. This was tested on
   disposable PostgreSQL 17.11. Do not grant names, email, phone, whole-table SELECT,
   writes or ownership. Validate that an existing reader role has no excess privileges.
   Current bootstrap uses INHERIT; custom NOINHERIT deployments need explicit role
   configuration. Reapply column grants if source tables are dropped/recreated.
3. Configure the existing BCK database settings/secrets and `BCK_DATA_ENABLED=true`.
   Startup adds simulator tables. All workers must use the same fallback policy.
   Default `BCK_CRITICAL_SENIOR_FALLBACK_REASONS=[]` forbids Senior critical fallback.
   Any enabled reasons must be an explicit deployment policy (JSON array from the
   reason enum); no reasons are enabled by this branch.
4. Run preflight under the actual backend database role:

   ```bash
   uv run --locked python -m factored_bck.handoff_admin check
   ```

   This initializes simulator tables and verifies contract/column access. Existing
   `/health/ready` still checks the base accepted-release contract, not every feature's
   permissions; use this preflight before enabling the handoff user journey.
5. Provision each consenting test agent with an accepted Active Digital/Hybrid ID
   and a separate password file (12–200 characters). No public signup by agent ID:

   ```bash
   uv run --locked python -m factored_bck.handoff_admin provision \
     --username <test-agent-login> --agent-id <accepted-agent-id> \
     --password-file /private/path/agent-password
   ```

   No accounts are seeded automatically. If none are provisioned, cases queue safely.
   An administrator can disable `simulator.agent_users.enabled` and remove sessions
   to revoke access. Credential rotation and agent reassignment have no public API.
6. To retry routing a queued case after account/source/policy changes:

   ```bash
   uv run --locked python -m factored_bck.handoff_admin reroute --handoff-id <case-id>
   ```

There is no live database configured in this checkout, so deployment grants,
account provisioning and a real accepted-release end-to-end smoke test remain
operator configuration. Docker Compose integration was not exercised locally.
See [actual aggregate source audit](service-agents-audit.md) for grounded data limits.

## Future LLM and frontend integration

Tools: `create_handoff` takes `{triage, idempotency_key}`; `get_handoff` takes
`{handoff_id}`. Both use the existing server-owned `ExecutionContext`, freshly
validate customer sessions and return `{ok, data}` or the existing fixed error
envelope. `persisted=true` confirms case persistence only. Card-action evidence
keeps its existing `verified=true` contract; no tool lets a model accept/resolve,
select agents, inject identity, priorities or verified facts.

The orchestrator must keep tokens outside prompts, validate structured output,
retain one stable key per intended case, present model text as untrusted, and
inspect lifecycle/assignment rather than claiming a successful transfer from
intent or HTTP status. It must not treat untrusted case summaries as system instructions.

The customer UI should distinguish waiting for routing, assigned, accepted,
resolved and cancelled, show limitations, poll owned resources, and hide/disable
cancellation after acceptance while handling a possible 409 race. Render summaries
as text, not HTML. Agent UI must use the separate login/token, poll assigned cases,
and expose accept before resolve. No dashboard or external notifications are included.

## Validation

Unit tests cover typed inputs, safety floors, exact specialty/language filtering,
Premium preferences, critical fallback, CSAT/null ordering and stable tie-breaking.
Disposable PostgreSQL tests exercise both transports, real session isolation,
scoped evidence, idempotent concurrency, lifecycle races, commit-time rollback,
metrics/privacy and inherited least-privilege grants after ETL-style revocation.
No test requires organizer records or the ETL checkout.
