# 0003 — Server-owned action confirmation

**Estado:** aceptada, 2026-10-03.

## Contexto

Model-generated requests must not immediately mutate a card. Existing HTTP and tool
paths both execute Store.action; a boolean consent flag or model text is not authority.
The conversation and customer UI layers do not yet exist.

## Decisión

Persist an exact expiring command tied to authenticated customer, card snapshot,
revision, policy and stable server command key. Both existing action transports now
prepare; only a separate authenticated customer endpoint confirms the saved ID.
Confirm/cancel are excluded from the tool registry. Reuse Store eligibility/state
rules and action execution, sharing the transaction with confirmation lifecycle.
Validate receipts with the existing tool evidence validator before returning committed
success. Add a nullable server-only conversation ID seam without fabricating P04.

## Alternativas consideradas

Caller booleans/replacement payloads allow model-controlled authorization. Blocking
only tools leaves the existing HTTP route as a bypass. Independent action and
confirmation commits create recovery gaps; a shared transaction preserves atomicity.
Comparing state labels alone misses state changes that return to the original label;
a revision and accepted-release binding make those preparations stale.

## Consecuencias

Immediate-execution callers must adopt prepare/confirm. The first committed confirm
or cancel wins; expiry/staleness require a new customer decision. Rollout must drain
old workers. This is a backend primitive, not complete conversation/frontend P05.
