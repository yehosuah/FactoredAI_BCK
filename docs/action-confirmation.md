# Server-side card action confirmation

This is the **backend confirmation primitive**, not full P05. There is no conversation
store, conversation authorization, human-intent classifier, frontend or LLM provider.

## Trust and architecture

`Confirmations` owns pending card commands and their lifecycle. It accepts a backend
customer session, never caller-supplied customer identity. `prepare` is available to
model tools; `confirm` and `cancel` are available only to the authenticated customer
transport. They are deliberately absent from `ToolDispatcher.catalog()` and dispatch.
A model cannot confirm with a boolean, prose, another action payload, or a tool call.

The HTTP transport is the eventual customer UI seam. An authenticated POST to the
specific confirmation ID is the explicit confirmation event. The future host must
send it only for a direct customer decision, never model output. The backend cannot
prove a human clicked without that trusted host/UI integration. Do not give an LLM
raw bearer credentials, arbitrary HTTP capability, Python access or `Store.action`.
Possession of a confirmation ID alone grants nothing: every operation authenticates
and scopes the row to the session's customer.

`Store.preview_action` shares the existing ownership, eligibility, transaction lookup
and `next_state` rules with `Store.action`; preparation performs no banking writes.
Confirmation calls **the existing `Store.action`**, using the saved exact command and
server-generated command key in a caller-owned transaction. The receipt validator
extracted from ToolDispatcher is shared in `evidence.verified_action_evidence`.
It requires matching product, action, outcome, successful simulated receipt and
request ID when appropriate. Confirmation also checks that the receipt and exact
payload exist in `simulator.actions` under the saved command key. The card change,
action evidence and executed confirmation commit atomically. No success response
leaves the module until that outer transaction commits.

`Store.action` remains a trusted internal execution primitive for backend code and
existing integrations/tests. Neither HTTP card actions nor model dispatch calls it
directly anymore. No generic `/tools/execute` or model confirmation tool exists.

## Schema and immutable command

Startup, under the existing Store initialization lock, adds:

- `simulator.action_confirmations`: confirmation ID, authenticated customer,
  client preparation key, unique backend execution key, exact command JSON,
  prepared card-state snapshot, optional server conversation ID, policy version,
  lifecycle state, creation/expiry/finish timestamps, linked action ID and evidence.
- `simulator.card_states.revision`: additive bigint default 0. State-changing
  `Store.action` updates increment it, including changes that later return to the
  original state. No previous state/evidence is reset.

Unique `(customer_id, preparation_key)` serializes preparation identity. The
backend generates an immutable `confirmation:<confirmation_id>` command key when
preparing, then uses that same key for execution and retries. This namespace is
separate from historical direct-action keys, preventing accidental reuse of old
action receipts as newly confirmed commands. Foreign-key/unique/check constraints
bind executed confirmations to one action and enforce terminal-state evidence.
No session token or token hash is stored in the confirmation.

The snapshot includes accepted release ID, simulator state, revision (or explicit
absence of overlay), source kind and source status. Command product, action,
transaction/date parameters, customer scope, expiry and policy are server-owned
once prepared. No replacement payload is accepted at confirm/cancel.

## HTTP contract and compatibility change

All routes require the existing customer bearer session. Agent sessions fail.
Responses and errors use the existing request-ID conventions. New confirmation
routes return `Cache-Control: no-store` on successful responses.

| Route | Behavior |
| --- | --- |
| `POST /me/action-confirmations` | Prepare a strict `CardCommand` using `Idempotency-Key` |
| `GET /me/action-confirmations/{confirmation_id}` | Read this customer's saved command/status |
| `POST /me/action-confirmations/{confirmation_id}/confirm` | Execute only the stored command; no body or `{}` |
| `POST /me/action-confirmations/{confirmation_id}/cancel` | Cancel pending command; no body or `{}` |
| `POST /me/cards/{product_id}/actions` | **Now prepares only**, sharing the same preparation key namespace |

Prepare body:

```json
{"product_id":"owned-card-id","action":"pause"}
```

Actions: `block`, `pause`, `reactivate`, `activate`, `replacement`,
`unrecognized-charge`. Only unrecognized-charge requires `transaction_id`
(1–30 nonblank characters) and `process_date` (exact valid `YYYY-MM-DD`). The other
actions reject those parameters. Product IDs are 1–100 nonblank characters.
Idempotency-Key is 1–100 characters from `[A-Za-z0-9_.:-]`. Fields are strict;
extra fields such as `customer_id`, `confirmed`, `user_confirmed`, `conversation_id`
or evidence are rejected. Confirmation IDs are 32 lowercase hexadecimal characters.

Preparation returns `confirmation_id`, `command`, `prepared_state`, `policy_version`,
`status=pending`, `created_at`, `expires_at`, `finished_at=null`, `simulated=true`,
`confirmation_required=true`, `verified=false`. It never includes action evidence.
Successful confirmation returns the same resource with `status=executed`,
`confirmation_required=false`, `verified=true` and the allowlisted committed
`evidence`. Executed replays contain historical evidence, not a claim about current
card state. Replacement and charge review still only register simulated requests:
no refund, reversal, delivery, issuance, or fraud adjudication is implied.

This intentionally changes the previous immediate-execution HTTP/tool contract.
Clients must inspect `status`/`verified`, display the stored command, request a
separate customer decision and preserve the returned confirmation ID. HTTP 200
for preparation does not mean a card action occurred. Existing read-only tools,
card read routes and human-handoff behavior retain their contracts.

## Lifecycle, expiry and stale state

`pending → executed | cancelled | expired | stale`. All four terminal states stay
terminal. There is no auto-execution, replacement of a saved command, refresh of its
expiry or automatic creation of a new confirmation on rejection.

`BCK_CONFIRMATION_SECONDS` is server-controlled: default 300 seconds, permitted
30–900. Creation and expiry use PostgreSQL wall time. Pending expiry is materialized
lazily on access/replay/confirm/cancel; no background worker is needed. Confirm
checks wall time after acquiring locks and again immediately before execution.
Executed records do not expire their historical evidence. Duplicate successful
confirmation returns the same receipt after reauthentication and ownership recheck.
Cancellation is idempotent only for already-cancelled records; cancellation after
execution returns 409 and cannot undo an action.

Before new execution the backend rereads ownership, snapshot and existing Store
eligibility. Any source-release change, relevant state/revision change, policy
version mismatch or lost card/transaction ownership makes a pending command stale.
Even Active → Paused → Active invalidates the original preparation. A stale or
expired command cannot execute: make a new preparation with a new key and obtain a
new customer decision. Source-release invalidation is deliberately conservative,
including team fixture cards. Database/source unavailability fails safely without
claiming execution and is retryable with the same saved ID.

Missing/other-customer IDs return 404, bad/missing/expired sessions 401, invalid
inputs 422, terminal/stale/expiry/idempotency conflicts 409. Unexpected database or
receipt-validation failures return sanitized 500; unavailable accepted contracts
may return 503. To inspect the persisted reason after 409, GET the confirmation.
Model tools use their existing fixed error envelope and never expose exception data.

## Idempotency and concurrency

The same customer preparation key and command return the same resource, without
resetting expiry or status. Changed product/action/parameters/server conversation
scope conflict with 409. Different customers have separate key namespaces.

All confirmation operations use the existing per-customer transaction advisory lock
shared with Store actions, followed by the confirmation row lock. This also covers
first-card-overlay creation and prevents races between state checks and ordinary
Store writes. Sessions are revalidated after waiting for the lock and locked for
that transaction, so committed revocations/expiry before authorization are rejected.
Confirmation/cancellation are serialized: the first successful terminal transition
commits, and the loser returns 409. Concurrent duplicate confirmations yield one
committed action. Two pending commands against the same card revision cannot both
change its state after the first invalidates the second.

Action mutations, receipts and executed status share one PostgreSQL transaction.
Receipt mismatch, persistence failure or commit-time error rolls them back together.
A lost network response can leave the caller uncertain about commit: retry the same
confirmation ID; never manufacture a new command or success from intent. Direct
administrative SQL must respect these locks and revision semantics; bypassing the
application database contract is outside the concurrency guarantee.

## Future conversation integration and deployment

`Confirmations.prepare(..., conversation_id=...)` is a bounded, optional **trusted
server-code** seam. It is not in HTTP bodies, tool arguments or ExecutionContext.
Today production calls leave it null. There is no fabricated conversation row or
claim of conversation authorization. A future conversation module must resolve its
own authenticated conversation, supply that ID, bind/validate conversation scope
on customer events and prevent model-generated text from authorizing confirmation.
It must persist pending-ID associations and handle expiry/retries across UI turns.

Deploy all serving workers with this version together; old workers still expose
immediate execution and would bypass the new transport policy. Drain old workers
before opening the confirmation journey. Startup applies additive simulator DDL;
no new ETL column grants or external infrastructure are needed. Configure TTL
consistently across workers, retain simulator data, and run a deployment smoke test
with the actual database/credentials. Local tests use disposable PostgreSQL only.
No new aggregate action is recorded during preparation, cancellation or rejection;
existing action metrics and handoff evidence see only confirmed committed actions.
Retention/cleanup, frontend rendering, conversation persistence/authorization,
customer-event provenance beyond bearer transport, and provider integration remain
out of scope. **Full P05 is not complete.**
