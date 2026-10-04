# 0004 — Atomic conversation turns with injected proposals

**Estado:** aceptada, 2026-10-04.

## Contexto

P04 needs ordered durable conversations and correlation with tools, confirmations
and handoffs. The ML implementation is an external dependency. Adapter proposals
cannot establish identity, ownership, authorization or successful banking effects.

## Decisión

Use one synchronous conversation module behind three customer HTTP endpoints and
one injected asynchronous proposal interface. Validate each result against the
versioned strict contract before calling the existing ToolDispatcher. Keep only
one proposal per turn; use an explicit deterministic stub for engineering evidence.

Serialize per customer using the existing advisory lock order. Join confirmation
preparation and handoff creation to the turn transaction through host-only optional
connection parameters; preserve standalone capability callers. Persist audit events
and newly prepared capabilities atomically, and use stable request keys for replay.
Reconcile committed confirmation/case states on reconnect. Preserve model text as
untrusted; only existing verified receipts establish action completion.

## Alternativas consideradas

Independent commits can leave prepared confirmations/cases unreferenced after a
turn failure. A durable asynchronous workflow/outbox could reconcile that gap but
adds machinery beyond this packet. A provider-specific interface would block the
backend engineering work on a delivery that is not present.

## Consecuencias

The customer lock remains held during a bounded, cancellation-cooperative adapter
call. This limits per-customer parallelism and requires nonblocking provider I/O;
a real adapter needs operational validation. Reconnect may append status events.
The executable BCK contract does not certify P00/P01 integration or real ML quality.
See `../conversations.md` for lifecycle, limits and dependency evidence.
