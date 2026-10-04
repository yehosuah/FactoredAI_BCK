"""Server-owned card commands; customer confirmation is outside the LLM tool catalog."""

from contextlib import contextmanager
from datetime import date
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from factored_bck.evidence import verified_action_evidence
from factored_bck.store import encode

POLICY_VERSION = "card-confirmation-v1"
ConfirmationId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
CommandKey = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")]
ConversationId = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]


class ActionParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    action: Literal[
        "block", "pause", "reactivate", "activate", "replacement", "unrecognized-charge"
    ]
    transaction_id: str | None = Field(default=None, min_length=1, max_length=30, pattern=r"\S")
    process_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", max_length=10)

    @model_validator(mode="after")
    def action_parameters(self):
        if self.action == "unrecognized-charge":
            if self.transaction_id is None or self.process_date is None:
                raise ValueError("charge_parameters_required")
            date.fromisoformat(self.process_date)
        elif self.transaction_id is not None or self.process_date is not None:
            raise ValueError("unexpected_action_parameters")
        return self


class CardCommand(ActionParameters):
    product_id: str = Field(min_length=1, max_length=100, pattern=r"\S")

    def store_arguments(self):
        return (
            self.product_id,
            self.action,
            self.transaction_id,
            date.fromisoformat(self.process_date) if self.process_date else None,
        )


class Confirmations:
    """Prepare may be called by tools. Confirm/cancel belong to trusted customer transport.

    No caller-provided principal, execution flag, replacement payload or model text grants
    confirmation authority. The host must never expose confirm as an LLM capability.
    """

    def __init__(self, store):
        self.store = store

    @contextmanager
    def _transaction(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 200:
            raise HTTPException(401)
        initial = self.store.session(token)
        with self.store.connect() as pg:
            # Same order for every operation and Store.action: customer lock, then row lock.
            pg.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,7236148203))",
                (initial["customer_id"],),
            )
            user = self.store.session(token, connection=pg, lock=True)
            if user["customer_id"] != initial["customer_id"]:
                raise HTTPException(401)
            yield pg, user

    def _snapshot(self, pg, user, command, release_id):
        card = self.store._card(pg, release_id, user["customer_id"], command.product_id)
        overlay = pg.execute(
            "SELECT revision FROM simulator.card_states WHERE product_id=%s AND customer_id=%s",
            (command.product_id, user["customer_id"]),
        ).fetchone()
        return {
            "release_id": release_id,
            "state": card["simulator_state"],
            "revision": overlay["revision"] if overlay else None,
            "source_kind": card["source_kind"],
            "source_status": card["product_status"],
        }

    @staticmethod
    def _finish(pg, row, status):
        return pg.execute(
            "UPDATE simulator.action_confirmations SET status=%s,finished_at=clock_timestamp() "
            "WHERE confirmation_id=%s RETURNING *",
            (status, row["confirmation_id"]),
        ).fetchone()

    def _expire(self, pg, row):
        if row["status"] == "pending":
            expired = pg.execute(
                "SELECT clock_timestamp()>=%s AS expired", (row["expires_at"],)
            ).fetchone()
            if expired["expired"]:
                row = self._finish(pg, row, "expired")
        return row

    @staticmethod
    def _resource(row):
        result = {
            key: row[key]
            for key in (
                "confirmation_id",
                "command",
                "prepared_state",
                "policy_version",
                "status",
                "created_at",
                "expires_at",
                "finished_at",
            )
        }
        result.update(
            confirmation_required=row["status"] == "pending",
            verified=row["status"] == "executed",
            simulated=True,
        )
        if row["status"] == "executed":
            result["evidence"] = row["evidence"]
        return encode(result)

    def _load(self, pg, user, confirmation_id):
        row = pg.execute(
            "SELECT * FROM simulator.action_confirmations WHERE confirmation_id=%s "
            "AND customer_id=%s FOR UPDATE",
            (confirmation_id, user["customer_id"]),
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        return self._expire(pg, row)

    def prepare(self, token, command: CardCommand, idempotency_key, *, conversation_id=None):
        command = CardCommand.model_validate(command)
        key = TypeAdapter(CommandKey).validate_python(idempotency_key, strict=True)
        # Reserved seam for a future trusted server-owned conversation, never a tool/body field.
        if conversation_id is not None:
            conversation_id = TypeAdapter(ConversationId).validate_python(
                conversation_id, strict=True
            )
        if token and (
            token in command.model_dump_json() or token in key or token == conversation_id
        ):
            raise HTTPException(422)
        payload = command.model_dump()
        with self._transaction(token) as (pg, user):
            previous = pg.execute(
                "SELECT * FROM simulator.action_confirmations WHERE customer_id=%s "
                "AND preparation_key=%s FOR UPDATE",
                (user["customer_id"], key),
            ).fetchone()
            if previous:
                if previous["command"] != payload or previous["conversation_id"] != conversation_id:
                    raise HTTPException(409)
                result = self._resource(self._expire(pg, previous))
            else:
                release_id = self.store._current(pg)
                self.store.preview_action(pg, release_id, user, *command.store_arguments())
                snapshot = self._snapshot(pg, user, command, release_id)
                confirmation_id = uuid4().hex
                row = pg.execute(
                    "INSERT INTO simulator.action_confirmations(confirmation_id,customer_id,"
                    "preparation_key,command_key,command,prepared_state,conversation_id,"
                    "policy_version,status,expires_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'pending',"
                    "clock_timestamp()+make_interval(secs => %s)) RETURNING *",
                    (
                        confirmation_id,
                        user["customer_id"],
                        key,
                        "confirmation:" + confirmation_id,
                        Jsonb(payload),
                        Jsonb(snapshot),
                        conversation_id,
                        POLICY_VERSION,
                        self.store.settings.confirmation_seconds,
                    ),
                ).fetchone()
                result = self._resource(row)
        return result

    def get(self, token, confirmation_id):
        with self._transaction(token) as (pg, user):
            result = self._resource(self._load(pg, user, confirmation_id))
        return result

    def cancel(self, token, confirmation_id):
        with self._transaction(token) as (pg, user):
            row = self._load(pg, user, confirmation_id)
            if row["status"] == "pending":
                row = self._finish(pg, row, "cancelled")
            result = self._resource(row)
        if row["status"] != "cancelled":
            raise HTTPException(409)
        return result

    def confirm(self, token, confirmation_id):
        with self._transaction(token) as (pg, user):
            row = self._load(pg, user, confirmation_id)
            command = CardCommand.model_validate(row["command"])
            if row["status"] == "executed":
                # Replay is historical evidence, not a new action; retain current ownership checks.
                self.store._card(
                    pg, self.store._current(pg), user["customer_id"], command.product_id
                )
            elif row["status"] == "pending":
                release_id = self.store._current(pg)
                try:
                    current = self._snapshot(pg, user, command, release_id)
                    if row["policy_version"] != POLICY_VERSION or current != row["prepared_state"]:
                        raise HTTPException(409)
                    _, _, outcome = self.store.preview_action(
                        pg, release_id, user, *command.store_arguments()
                    )
                except HTTPException as exc:
                    if exc.status_code not in (404, 409, 422):
                        raise
                    row = self._finish(pg, row, "stale")
                else:
                    # Check real wall time after blocking reads, not transaction-start now().
                    row = self._expire(pg, row)
                    if row["status"] == "pending":
                        product_id, action, transaction_id, process_date = command.store_arguments()
                        receipt = self.store.action(
                            user,
                            product_id,
                            action,
                            row["command_key"],
                            transaction_id,
                            process_date,
                            connection=pg,
                            release_id=release_id,
                        )
                        evidence = verified_action_evidence(action, product_id, outcome, receipt)
                        # Require the receipt to be the action written in this same transaction.
                        persisted = pg.execute(
                            "SELECT result,payload FROM simulator.actions WHERE customer_id=%s "
                            "AND idempotency_key=%s AND action_id=%s",
                            (user["customer_id"], row["command_key"], evidence["action_id"]),
                        ).fetchone()
                        if (
                            not persisted
                            or persisted["payload"] != row["command"]
                            or persisted["result"] != evidence
                        ):
                            raise ValueError("unverified_persisted_action")
                        row = pg.execute(
                            "UPDATE simulator.action_confirmations SET status='executed',"
                            "finished_at=clock_timestamp(),action_id=%s,evidence=%s "
                            "WHERE confirmation_id=%s RETURNING *",
                            (evidence["action_id"], Jsonb(evidence), confirmation_id),
                        ).fetchone()
            result = self._resource(row)
        # Transaction has committed; neither a failed validation nor failed commit yields success.
        if row["status"] != "executed":
            raise HTTPException(409)
        return result
