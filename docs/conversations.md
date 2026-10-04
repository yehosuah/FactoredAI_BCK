# Persistent conversations and injected adapter (P04)

## Scope and dependency status

This backend implements the P04 engineering interface from the October 3–5 agent
sprint: authenticated persistent ES/PT conversations, bounded injected proposals,
existing tools/confirmations/handoffs, and a clearly labeled deterministic stub.
It does not implement a model, prompts, intent classification, retrieval, frontend,
streaming, or new banking operations. No provider credentials are configured here.

The inspected base is `033dccbe4ef84f447f093755908a14a87e14bf11`. It contains
server-owned confirmations, ToolDispatcher, and persistent handoffs with local
recovery. It does **not** contain evidence of an accepted cross-repository P00
conversation contract or P01's complete public nine-table ETL fixture boot. The
contract below is executable in BCK and ready for consumer review, not evidence
that FRT/ETL consumers have accepted it. Tests create private synthetic PostgreSQL
fixtures directly; they do not claim the P01 Extract/Transform/Load acceptance.
Existing organizer-provenance behavior in source card reads belongs to P01 and is
unchanged. Full sprint acceptance remains blocked on those dependency gates and
integrated verification; a passing stub suite is engineering evidence only.

## HTTP contract: conversation-adapter-v1

All routes require the existing opaque customer bearer session. Ownership comes
only from that session, never a request field. Another customer's conversation
returns 404; absent/expired/revoked or agent sessions return 401. Bodies forbid
unknown fields, including customer identity, chosen conversation ID, evidence,
confirmation authority and execution flags. IDs are backend-generated 32-character
lowercase hex. Responses carry `Cache-Control: no-store` and existing request IDs.

| Method/path | Input | Result |
| --- | --- | --- |
| `POST /me/conversations` | `{"language":"es"}` or `pt`; required `Idempotency-Key` | Owned persistent conversation |
| `GET /me/conversations/{id}` | Optional `after=0`, `limit=100` (1–100) | Reconnect, reconcile owned capability status, ordered event page |
| `POST /me/conversations/{id}/turns` | `{"message":"...","language":"pt"}`; language optional; required `Idempotency-Key` | Persisted turn and its events, with `submitted_turn_id` |

Keys are 1–100 characters from `[A-Za-z0-9_.:-]`. Message is nonblank, at most
2,000 characters. Creation key scope is customer; turn key scope is conversation.
Identical retry returns the original identity and current state without invoking
an adapter/tool again; changed input with the same key is 409. A turn retry retains
the originally submitted payload, including omitted language. A later language
change does not invalidate that retry or change any prepared/confirmed command.

A conversation stores up to 100 turns. A new turn above that bound returns 409;
reads and idempotent retries remain available. The state includes language,
creation/update timestamps, current adapter descriptor, `last_sequence`, events,
and `next_after`. Follow `next_after` to obtain the next ordered page. POST returns
a page starting at the submitted turn; GET from zero reads the full retained history
through pagination. Adapter descriptor on each event records that turn's provider,
version and mode, even if the host subsequently injects a different adapter.

## Adapter seam and stub demonstration

`create_app(..., adapter=adapter)` injects an implementation of `MLAdapter` with an
`AdapterInfo` descriptor and `async propose(AdapterContext) -> dict`. The context
contains contract version, ES/PT language, and at most the latest 20 user/assistant
messages, with a truncation flag. It excludes session tokens, customer/account
identity, database handles and executable capabilities. Text is untrusted. This
minimal contract does not provide full transcript retention or tool-result context
to a model; ordered tool results remain available in the customer conversation.

Implementations must use nonblocking I/O, honor cancellation and set bounded
provider deadlines. The host uses `asyncio.wait_for`, with
`BCK_ADAPTER_TIMEOUT_SECONDS=5` by default (0.01–30). In-process Python is not a
sandbox: a blocking or cancellation-suppressing adapter violates this injected
interface. A real provider implementation and its operational testing are external
work. There are zero automatic provider/tool retries in this packet.

Exactly one proposal is allowed per turn. The backend validates strict schemas
and a 16 KiB serialized output limit before orchestration:

```json
{"kind":"answer","text":"Untrusted assistant text"}
{"kind":"clarification","question":"Which card needs help?"}
{"kind":"tool_request","name":"get_cards","arguments":{}}
{"kind":"tool_request","name":"pause_card","arguments":{"product_id":"<owned-card>"}}
{"kind":"human_handoff","triage":{"reason":"card_support","severity":"low","required_specialty":null,"minimum_experience":"Junior","language":"es","summary":"Needs human review"}}
```

Allowed proposed tools are `get_cards`, `get_card`, `get_movements`, `block_card`,
`pause_card`, `reactivate_card`, `activate_card`, `request_replacement`, and
`register_unrecognized_charge`. Their existing strict argument schemas apply,
except idempotency keys are generated by the conversation backend and cannot be
supplied by the adapter. Handoff is its own typed proposal using existing Triage.
Unknown tools, identity fields, confirmation commands, extra verified/evidence
fields and invalid arguments are rejected before calling the dispatcher. Invalid calendar dates and handoff languages that
disagree with the conversation language are also rejected. The
backend still revalidates ownership and eligibility through existing capabilities.

`BCK_CONVERSATION_ADAPTER=disabled` is the default: turns persist an
`adapter_unavailable` error, with no simulated provider success. For an explicit
engineering demonstration set `BCK_CONVERSATION_ADAPTER=stub` with the existing
data/session setup. The descriptor is `deterministic-engineering-stub`, version
`1`, mode `stub`. Exact commands are:

- `/cards`: proposes `get_cards`.
- `/pause <owned-card-id>`: proposes preparation of a pause confirmation.
- `/clarify`: fixed ES/PT pause-versus-loss question; no tool call.
- `/handoff`: fixed labeled engineering triage in the conversation language.
- Anything else: fixed ES/PT answer that explains the message itself executes no banking action.

These are engineering commands, not ML intent recognition. Tests exercise this
stub through HTTP in both languages; use `uv run --locked pytest
tests/test_conversations.py` with local `initdb`/`pg_ctl` to reproduce the private
synthetic HTTP demonstration without organizer data or a provider.

## Storage, ordering and authority

Startup adds `simulator.conversations`, `conversation_turns` and
`conversation_events`, plus nullable `handoffs.conversation_id`. Existing
confirmation `conversation_id` is reused. Original standalone HTTP/tool behavior
remains compatible. Ordered events retain immutable event/turn IDs, monotonic
per-conversation sequence, kind, trust, typed resource references, data and timestamp.
For tool-related events, `event_id` is the stable tool invocation/result reference;
`turn_id` links it to the user message and provider descriptor. Handoff IDs are logical references validated at creation/read; confirmation and
turn references have database foreign keys.

A customer advisory lock and owned conversation row lock serialize turns. The lock
order matches confirmations/actions. The adapter runs while that bounded transaction
holds the customer's lock; this deliberately favors consistency over per-customer
parallelism. Local SQL operations use a five-second lock timeout and ten-second
statement timeout. Existing read tools keep their existing Store connection behavior;
this packet does not claim a universal hard deadline for arbitrary backend code.

Tool preparation and handoff creation join the conversation transaction through
trusted host-only connection/ID parameters. A savepoint rolls back a tool failure
before a sanitized error event is stored. Final commit includes user message,
response/result references and any newly prepared confirmation or handoff together.
A commit failure cannot leave an orphan prepared capability. Session validity is
checked on entry, after adapter response and before commit; session share locks
serialize revocation. IDs/keys/credentials never come from adapter output.

`answer` and `clarification` are always `trust=untrusted`, `verified=false`.
Free-form claims such as “refund issued” are not bank facts and cannot create an
action or receipt. Clients must render that trust distinction and only present
committed typed backend evidence as success. This layer does not semantically
certify model prose or implement an ML safety classifier.

## Confirmation, handoff and evidence correlation

For mutations the ToolDispatcher calls existing `Confirmations.prepare` with the
backend-owned conversation ID and generated preparation key. A
`confirmation_prepared` event links conversation → turn → event → confirmation.
It means pending customer confirmation, never successful action execution. There
is no new confirmation boolean or model confirmation capability. Only the existing
separate customer `/me/action-confirmations/{id}/confirm` endpoint can execute.

Reconnect or the next turn reconciles linked confirmations against owned backend
rows. Changed status appends one `confirmation_status` event; an executed status
includes the existing verified receipt and `action_id`. Expired, cancelled and
stale statuses remain unverified. Repeated reconnect does not duplicate events.
Reconciliation happens in the same transaction, and may persist status events even
though the transport is GET. Current language cannot change the saved command.

Handoff proposals reuse existing persistent routing, account eligibility, queues
and local-admin recovery. The case stores its conversation ID, and a
`handoff_created` event links its turn and handoff ID. Its evidence includes only
executed confirmations from that conversation, optionally further scoped to its
card, rather than the customer's unrelated action history. The original capped
20-receipt snapshot and truncation flag still apply. Triage/context stays explicitly
untrusted, separate from verified backend evidence. Case status/assignment changes
append `handoff_status` on reconnect, including after local recovery. Conversation
history retains read results, failed attempts and unanswered clarification text;
this packet does not add an agent conversation viewer or claim full P08 support UI.

## Failure and replay behavior

A successfully persisted failed turn returns HTTP 200 with a backend `error` event
and `verified=false`, using `adapter_unavailable`, `adapter_timeout`,
`adapter_failure`, `invalid_adapter_output`, or `tool_failure` (with the existing
sanitized tool error code). Adapter-raised cancellation is recorded as `adapter_failure`. No provider exception, SQL message or credentials are
returned. No error fallback invokes a mutation. A repeated failed turn key returns
that persisted failure; a new explicit user submission requires a new key.

Transport/schema errors retain existing 401/404/409/422/500 envelopes. A database
failure that prevents commit returns no persisted-success state. If the HTTP result
is lost, retry the same conversation/turn key and payload or reconnect; a transport
failure alone is neither action success nor proof the commit failed. No generic
`/tools/execute`, agent recovery route, provider credentials or real model is added.

## Validation record

On the final P04 working revision, `UV_NO_EDITABLE=1 make check` passed all 329
backend tests, including 52 dedicated conversation cases, Ruff and formatting.
`git diff --check` passed. Tests use private temporary PostgreSQL fixtures, never
organizer records or a shared integration database. Regular-package installation
was used to avoid the local hidden editable-path issue, and every installed Python
module was compared byte-for-byte against this checkout before accepting results.

The author completed separate standards and specification reviews. Three additional
failure cases (invalid calendar dates, handoff language mismatch, adapter-raised
cancellation) were fixed and covered. No findings remain in that direct review.
Both independent review agents were attempted but hit the account usage limit;
independent review approval is unavailable. P00/P01 acceptance and real ML/provider
validation remain outside this evidence, as described above.
