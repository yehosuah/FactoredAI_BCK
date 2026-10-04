"""Owned conversations: one serialized transaction for a turn and prepared capabilities."""

import asyncio
import json
from contextlib import contextmanager
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter, ValidationError

from factored_bck.conversation_contract import (
    MUTATING_TOOLS,
    PROPOSAL,
    AdapterContext,
    AdapterInfo,
    Answer,
    Clarification,
    ContextMessage,
    ConversationState,
    CreateConversation,
    HumanHandoff,
    Identifier,
    Key,
    SubmitTurn,
)
from factored_bck.store import encode
from factored_bck.tools import ExecutionContext


class Conversations:
    def __init__(self, store, tools, confirmations, handoffs, adapter=None):
        self.store = store
        self.tools = tools
        self.confirmations = confirmations
        self.handoffs = handoffs
        self.adapter = adapter
        self.info = (
            AdapterInfo.model_validate(adapter.info)
            if adapter
            else AdapterInfo(provider="none", version="none", mode="disabled")
        )

    @contextmanager
    def _transaction(self, token):
        initial = self.store.session(token)
        with self.store.connect() as pg:
            pg.execute("SET LOCAL lock_timeout='5s'")
            pg.execute("SET LOCAL statement_timeout='10s'")
            # Same lock order as confirmations/actions: customer first, then owned rows.
            pg.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,7236148203))",
                (initial["customer_id"],),
            )
            user = self.store.session(token, connection=pg, lock=True)
            if user["customer_id"] != initial["customer_id"]:
                raise HTTPException(401)
            yield pg, user
            self.store.session(token, connection=pg)  # Wall-clock expiry before commit too.

    @staticmethod
    def _owned(pg, user, conversation_id):
        TypeAdapter(Identifier).validate_python(conversation_id, strict=True)
        row = pg.execute(
            "SELECT * FROM simulator.conversations WHERE conversation_id=%s AND customer_id=%s "
            "FOR UPDATE",
            (conversation_id, user["customer_id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404)
        return row

    def _event(
        self,
        pg,
        conversation_id,
        turn_id,
        kind,
        data,
        *,
        trust="backend",
        confirmation_id=None,
        handoff_id=None,
    ):
        row = pg.execute(
            "INSERT INTO simulator.conversation_events(event_id,conversation_id,turn_id,sequence,"
            "kind,trust,data,confirmation_id,handoff_id) SELECT %s,%s,%s,"
            "coalesce(max(sequence),0)+1,%s,%s,%s,%s,%s FROM simulator.conversation_events "
            "WHERE conversation_id=%s RETURNING *",
            (
                uuid4().hex,
                conversation_id,
                turn_id,
                kind,
                trust,
                Jsonb(encode(data)),
                confirmation_id,
                handoff_id,
                conversation_id,
            ),
        ).fetchone()
        pg.execute(
            "UPDATE simulator.conversations SET updated_at=clock_timestamp() "
            "WHERE conversation_id=%s",
            (conversation_id,),
        )
        return row

    def _reconcile(self, pg, user, conversation_id):
        """Persist status changes from owned committed capabilities, never adapter claims."""
        references = pg.execute(
            "SELECT DISTINCT ON (confirmation_id) * FROM simulator.conversation_events "
            "WHERE conversation_id=%s AND confirmation_id IS NOT NULL "
            "ORDER BY confirmation_id,sequence DESC",
            (conversation_id,),
        ).fetchall()
        for event in references:
            row = self.confirmations._load(pg, user, event["confirmation_id"])
            if row["conversation_id"] != conversation_id:
                raise RuntimeError("confirmation_scope_mismatch")
            data = self.confirmations._resource(row)
            if event["data"]["result"] != data:
                self._event(
                    pg,
                    conversation_id,
                    event["turn_id"],
                    "confirmation_status",
                    {"result": data},
                    confirmation_id=event["confirmation_id"],
                )
        references = pg.execute(
            "SELECT DISTINCT ON (handoff_id) * FROM simulator.conversation_events "
            "WHERE conversation_id=%s AND handoff_id IS NOT NULL "
            "ORDER BY handoff_id,sequence DESC",
            (conversation_id,),
        ).fetchall()
        for event in references:
            row = pg.execute(
                "SELECT * FROM simulator.handoffs WHERE handoff_id=%s AND customer_id=%s "
                "AND conversation_id=%s",
                (event["handoff_id"], user["customer_id"], conversation_id),
            ).fetchone()
            if not row:
                raise RuntimeError("handoff_scope_mismatch")
            data = self.handoffs._resource(row)
            if event["data"]["result"] != data:
                self._event(
                    pg,
                    conversation_id,
                    event["turn_id"],
                    "handoff_status",
                    {"result": data},
                    handoff_id=event["handoff_id"],
                )

    def _resource(self, pg, user, conversation_id, *, after=0, limit=100, turn_id=None):
        row = self._owned(pg, user, conversation_id)
        events = pg.execute(
            "SELECT e.event_id,e.turn_id,e.sequence,e.kind,e.trust,e.data,e.confirmation_id,"
            "e.handoff_id,e.created_at,t.adapter "
            "FROM simulator.conversation_events e "
            "JOIN simulator.conversation_turns t USING(turn_id) "
            "WHERE e.conversation_id=%s AND e.sequence>%s "
            "ORDER BY sequence LIMIT %s",
            (conversation_id, after, limit + 1),
        ).fetchall()
        last = pg.execute(
            "SELECT coalesce(max(sequence),0) AS sequence FROM simulator.conversation_events "
            "WHERE conversation_id=%s",
            (conversation_id,),
        ).fetchone()["sequence"]
        return ConversationState.model_validate(
            encode(
                {
                    "conversation_id": conversation_id,
                    "language": row["language"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "adapter": self.info.model_dump(),
                    "events": events[:limit],
                    "next_after": events[limit - 1]["sequence"] if len(events) > limit else None,
                    "last_sequence": last,
                    "submitted_turn_id": turn_id,
                }
            )
        ).model_dump(mode="json")

    def create(self, token, body: CreateConversation, key):
        body = CreateConversation.model_validate(body)
        key = TypeAdapter(Key).validate_python(key, strict=True)
        if token in key:
            raise HTTPException(422)
        with self._transaction(token) as (pg, user):
            row = pg.execute(
                "SELECT * FROM simulator.conversations WHERE customer_id=%s AND creation_key=%s",
                (user["customer_id"], key),
            ).fetchone()
            if row:
                if row["initial_language"] != body.language:
                    raise HTTPException(409)
            else:
                row = pg.execute(
                    "INSERT INTO simulator.conversations(conversation_id,customer_id,creation_key,"
                    "initial_language,language) VALUES(%s,%s,%s,%s,%s) RETURNING *",
                    (uuid4().hex, user["customer_id"], key, body.language, body.language),
                ).fetchone()
            result = self._resource(pg, user, row["conversation_id"])
        return result

    def get(self, token, conversation_id, *, after=0, limit=100):
        if not 0 <= after or not 1 <= limit <= 100:
            raise HTTPException(422)
        with self._transaction(token) as (pg, user):
            self._owned(pg, user, conversation_id)
            self._reconcile(pg, user, conversation_id)
            result = self._resource(pg, user, conversation_id, after=after, limit=limit)
        return result

    def _context(self, pg, conversation_id, language):
        rows = pg.execute(
            "SELECT kind,data FROM simulator.conversation_events WHERE conversation_id=%s "
            "AND kind IN ('user_message','answer','clarification') "
            "ORDER BY sequence DESC LIMIT 21",
            (conversation_id,),
        ).fetchall()
        return AdapterContext(
            language=language,
            history_truncated=len(rows) > 20,
            messages=tuple(
                ContextMessage(
                    role="user" if row["kind"] == "user_message" else "assistant",
                    text=row["data"]["text"],
                )
                for row in reversed(rows[:20])
            ),
        )

    def _propose(self, context, token):
        if self.adapter is None:
            return None, "adapter_unavailable"

        async def bounded():
            return await asyncio.wait_for(
                self.adapter.propose(context), timeout=self.store.settings.adapter_timeout_seconds
            )

        try:
            raw = asyncio.run(bounded())
        except TimeoutError:
            return None, "adapter_timeout"
        except (Exception, asyncio.CancelledError):
            return None, "adapter_failure"
        try:
            if type(raw) is not dict:
                raise ValueError("invalid_adapter_output")
            serialized = json.dumps(raw, ensure_ascii=False, allow_nan=False)
            if len(serialized.encode()) > 16384 or token in serialized:
                raise ValueError("invalid_adapter_output")
            proposal = PROPOSAL.validate_python(raw, strict=True)
            if isinstance(proposal, HumanHandoff) and proposal.triage.language != context.language:
                raise ValueError("handoff_language_mismatch")
            return proposal, None
        except (ValidationError, ValueError, TypeError, RecursionError):
            return None, "invalid_adapter_output"

    def _orchestrate(self, pg, token, conversation_id, turn_id, proposal):
        if isinstance(proposal, (Answer, Clarification)):
            text = proposal.text if isinstance(proposal, Answer) else proposal.question
            self._event(
                pg,
                conversation_id,
                turn_id,
                proposal.kind,
                {"text": text, "verified": False},
                trust="untrusted",
            )
            return
        key = "conversation:" + turn_id
        if isinstance(proposal, HumanHandoff):
            name, arguments = (
                "create_handoff",
                {
                    "triage": proposal.triage.model_dump(),
                    "idempotency_key": key,
                },
            )
        else:
            name, arguments = proposal.name, dict(proposal.arguments)
            if name in MUTATING_TOOLS:
                arguments["idempotency_key"] = key
        result = self.tools.execute(
            name,
            arguments,
            context=ExecutionContext(session_token=token),
            conversation_id=conversation_id,
            connection=pg,
        )
        if not result["ok"]:
            self._event(
                pg,
                conversation_id,
                turn_id,
                "error",
                {
                    "code": "tool_failure",
                    "tool": name,
                    "tool_error": result["error"]["code"],
                    "verified": False,
                },
            )
            return
        data = result["data"]
        if name in MUTATING_TOOLS:
            kind, references = "confirmation_prepared", {"confirmation_id": data["confirmation_id"]}
        elif name == "create_handoff":
            kind, references = "handoff_created", {"handoff_id": data["handoff_id"]}
        else:
            kind, references = "tool_result", {}
        self._event(
            pg,
            conversation_id,
            turn_id,
            kind,
            {"tool": name, "arguments": arguments, "result": data},
            **references,
        )

    def submit(self, token, conversation_id, body: SubmitTurn, key):
        body = SubmitTurn.model_validate(body)
        key = TypeAdapter(Key).validate_python(key, strict=True)
        if token in body.model_dump_json() or token in key:
            raise HTTPException(422)
        with self._transaction(token) as (pg, user):
            conversation = self._owned(pg, user, conversation_id)
            previous = pg.execute(
                "SELECT * FROM simulator.conversation_turns WHERE conversation_id=%s "
                "AND idempotency_key=%s",
                (conversation_id, key),
            ).fetchone()
            if previous:
                if previous["payload"] != body.model_dump():
                    raise HTTPException(409)
                turn_id = previous["turn_id"]
            else:
                count = pg.execute(
                    "SELECT count(*) AS n FROM simulator.conversation_turns "
                    "WHERE conversation_id=%s",
                    (conversation_id,),
                ).fetchone()["n"]
                if count >= 100:
                    raise HTTPException(409)
                self._reconcile(pg, user, conversation_id)
                language = body.language or conversation["language"]
                turn_id = uuid4().hex
                pg.execute(
                    "INSERT INTO simulator.conversation_turns(turn_id,conversation_id,"
                    "idempotency_key,payload,language,adapter) VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        turn_id,
                        conversation_id,
                        key,
                        Jsonb(body.model_dump()),
                        language,
                        Jsonb(self.info.model_dump()),
                    ),
                )
                pg.execute(
                    "UPDATE simulator.conversations SET language=%s WHERE conversation_id=%s",
                    (language, conversation_id),
                )
                self._event(
                    pg,
                    conversation_id,
                    turn_id,
                    "user_message",
                    {"text": body.message, "language": language},
                    trust="untrusted",
                )
                proposal, error = self._propose(self._context(pg, conversation_id, language), token)
                self.store.session(token, connection=pg)  # Recheck expiry after provider delay.
                if error:
                    self._event(
                        pg, conversation_id, turn_id, "error", {"code": error, "verified": False}
                    )
                else:
                    self._orchestrate(pg, token, conversation_id, turn_id, proposal)
            self._reconcile(pg, user, conversation_id)
            first = pg.execute(
                "SELECT min(sequence) AS sequence FROM simulator.conversation_events "
                "WHERE turn_id=%s",
                (turn_id,),
            ).fetchone()["sequence"]
            result = self._resource(pg, user, conversation_id, after=first - 1, turn_id=turn_id)
        return result
