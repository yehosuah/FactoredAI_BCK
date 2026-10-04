"""Customer-scoped historical reads and transactional, persistent simulated actions."""

import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from factored_bck.pagination import MovementCursor
from factored_bck.security import (
    DUMMY_PASSWORD_HASH,
    next_state,
    password_hash,
    password_matches,
    token_digest,
)

CARD_TYPES = ("Tarjeta Crédito", "Tarjeta Débito")
SOURCE_CONTRACT_VERSION = "card-support-etl-v1"
STATE_MAP = {
    "Active": "ACTIVE",
    "Closed": "CLOSED",
    "Blocked": "BLOCKED",
    "Suspended": "INELIGIBLE",
}


def encode(value):
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


class Store:
    def __init__(self, settings):
        self.settings = settings

    def connect(self):
        cfg = self.settings
        return psycopg.connect(
            host=cfg.db_host,
            port=cfg.db_port,
            dbname=cfg.db_name,
            user=cfg.db_user,
            password=cfg.db_password_file.read_text().strip() if cfg.db_password_file else "",
            connect_timeout=5,
            row_factory=dict_row,
            application_name="factored_backend",
        )

    def initialize(self):
        with self.connect() as pg:
            pg.execute("SELECT pg_advisory_xact_lock(7236148202)")
            pg.execute(
                "CREATE TABLE IF NOT EXISTS simulator.users (username text PRIMARY KEY, "
                "password_hash text NOT NULL, customer_id text NOT NULL, source_kind text NOT NULL)"
            )
            pg.execute(
                "CREATE TABLE IF NOT EXISTS simulator.sessions (token_hash text PRIMARY KEY, "
                "username text NOT NULL REFERENCES simulator.users, "
                "expires_at timestamptz NOT NULL)"
            )
            pg.execute(
                "CREATE TABLE IF NOT EXISTS simulator.login_attempts "
                "(subject_hash text PRIMARY KEY, "
                "attempts integer NOT NULL, window_start timestamptz NOT NULL)"
            )
            pg.execute(
                "CREATE TABLE IF NOT EXISTS simulator.card_states (product_id text PRIMARY KEY, "
                "customer_id text NOT NULL, state text NOT NULL, "
                "updated_at timestamptz NOT NULL DEFAULT now())"
            )
            pg.execute(
                "CREATE TABLE IF NOT EXISTS simulator.actions (action_id text PRIMARY KEY, "
                "customer_id text NOT NULL, idempotency_key text NOT NULL, payload jsonb NOT NULL, "
                "result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), "
                "UNIQUE(customer_id,idempotency_key))"
            )
            pg.execute(
                "CREATE TABLE IF NOT EXISTS simulator.fixture_cards (product_id text PRIMARY KEY, "
                "customer_id text NOT NULL, product_type text NOT NULL, "
                "product_number text NOT NULL, "
                "currency text NOT NULL, current_balance numeric(15,2) NOT NULL, "
                "credit_limit numeric(15,2), product_status text NOT NULL, "
                "source_kind text NOT NULL)"
            )
            if self.settings.demo_password_file:
                pw = self.settings.demo_password_file.read_text().strip()
                if not 12 <= len(pw) <= 200:
                    raise ValueError("invalid_demo_secret")
                demo = pg.execute(
                    "SELECT * FROM simulator.users WHERE username='demo' FOR UPDATE"
                ).fetchone()
                if demo is None:
                    pg.execute(
                        "INSERT INTO simulator.users VALUES(%s,%s,%s,%s)",
                        ("demo", password_hash(pw), "TEAM-CUSTOMER-001", "team_synthetic"),
                    )
                else:
                    if (demo["customer_id"], demo["source_kind"]) != (
                        "TEAM-CUSTOMER-001",
                        "team_synthetic",
                    ):
                        raise ValueError("demo_identity_conflict")
                    if not password_matches(pw, demo["password_hash"]):
                        pg.execute(
                            "UPDATE simulator.users SET password_hash=%s WHERE username='demo'",
                            (password_hash(pw),),
                        )
                        pg.execute("DELETE FROM simulator.sessions WHERE username='demo'")
                fixtures = [
                    ("TEAM-CARD-ACTIVE", "ACTIVE"),
                    ("TEAM-CARD-PAUSED", "PAUSED"),
                    ("TEAM-CARD-PENDING", "PENDING_ACTIVATION"),
                    ("TEAM-CARD-BLOCKED", "BLOCKED"),
                ]
                for i, (card_id, state) in enumerate(fixtures):
                    pg.execute(
                        "INSERT INTO simulator.fixture_cards VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING",
                        (
                            card_id,
                            "TEAM-CUSTOMER-001",
                            "Tarjeta Crédito",
                            "TEAM-TEST-" + str(1000 + i),
                            "USD",
                            Decimal("125.50"),
                            Decimal("1000.00"),
                            state,
                            "team_synthetic",
                        ),
                    )
                    pg.execute(
                        "INSERT INTO simulator.card_states(product_id,customer_id,state) "
                        "VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                        (card_id, "TEAM-CUSTOMER-001", state),
                    )
                pg.execute(
                    "INSERT INTO simulator.fixture_cards VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (
                        "TEAM-CARD-OTHER",
                        "TEAM-CUSTOMER-OTHER",
                        "Tarjeta Débito",
                        "TEAM-TEST-9999",
                        "USD",
                        Decimal("20.00"),
                        None,
                        "ACTIVE",
                        "team_synthetic",
                    ),
                )

    def ready(self):
        with self.connect() as pg:
            return {"release_id": self._current(pg)}

    def _current(self, pg):
        row = pg.execute(
            "SELECT r.release_id,r.manifest->>'contract_version' AS contract_version "
            "FROM bank.current_release c JOIN bank.releases r USING(release_id) WHERE singleton"
        ).fetchone()
        if not row or row["contract_version"] != SOURCE_CONTRACT_VERSION:
            raise HTTPException(503)
        return row["release_id"]

    def login(self, username, password, peer):
        subject = token_digest(username + "|" + peer)
        with self.connect() as pg:
            attempt = pg.execute(
                "INSERT INTO simulator.login_attempts VALUES(%s,1,now()) "
                "ON CONFLICT(subject_hash) DO UPDATE SET attempts=CASE WHEN "
                "simulator.login_attempts.window_start<now()-interval '5 minutes' "
                "THEN 1 ELSE simulator.login_attempts.attempts+1 END, window_start=CASE "
                "WHEN simulator.login_attempts.window_start<now()-interval '5 minutes' "
                "THEN now() ELSE simulator.login_attempts.window_start END RETURNING attempts",
                (subject,),
            ).fetchone()
            user = pg.execute(
                "SELECT * FROM simulator.users WHERE username=%s FOR UPDATE", (username,)
            ).fetchone()
            allowed = attempt["attempts"] <= 10
            # Always perform scrypt, including unknown usernames. The user lock keeps
            # session issuance atomic with credential rotation and session revocation.
            matched = password_matches(
                password, user["password_hash"] if user is not None else DUMMY_PASSWORD_HASH
            )
            valid = user is not None and matched
            if not allowed or not valid:
                pg.commit()  # Failed attempts must persist before raising an HTTP exception.
                raise HTTPException(429 if not allowed else 401)
            token = secrets.token_urlsafe(32)
            expiry = datetime.now(UTC) + timedelta(seconds=self.settings.session_seconds)
            pg.execute(
                "INSERT INTO simulator.sessions VALUES(%s,%s,%s)",
                (token_digest(token), username, expiry),
            )
            pg.execute("DELETE FROM simulator.login_attempts WHERE subject_hash=%s", (subject,))
            return {
                "access_token": token,
                "token_type": "bearer",
                "expires_at": expiry.isoformat(),
                "mode": "test_simulator",
            }

    def session(self, token):
        with self.connect() as pg:
            user = pg.execute(
                "SELECT u.username,u.customer_id,u.source_kind FROM simulator.sessions s "
                "JOIN simulator.users u USING(username) WHERE token_hash=%s AND expires_at>now()",
                (token_digest(token),),
            ).fetchone()
            if not user:
                raise HTTPException(401)
            return user

    def logout(self, token):
        with self.connect() as pg:
            pg.execute("DELETE FROM simulator.sessions WHERE token_hash=%s", (token_digest(token),))
        return {"status": "logged_out"}

    @contextmanager
    def release(self):
        with self.connect() as pg:
            pg.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            yield pg, self._current(pg)

    def _card(self, pg, release_id, customer_id, product_id):
        card = pg.execute(
            "SELECT *,NULL::timestamp AS last_updated FROM simulator.fixture_cards "
            "WHERE product_id=%s AND customer_id=%s",
            (product_id, customer_id),
        ).fetchone()
        if not card:
            card = pg.execute(
                "SELECT product_id,customer_id,product_type,product_number,"
                "currency,current_balance,"
                "credit_limit,product_status,last_updated,'organizer_synthetic' AS source_kind "
                "FROM bank.products WHERE release_id=%s AND customer_id=%s AND product_id=%s "
                "AND product_type=ANY(%s)",
                (release_id, customer_id, product_id, list(CARD_TYPES)),
            ).fetchone()
        if not card:
            raise HTTPException(404)
        state = pg.execute(
            "SELECT state,customer_id FROM simulator.card_states WHERE product_id=%s", (product_id,)
        ).fetchone()
        if state and state["customer_id"] != customer_id:
            raise HTTPException(409)
        card["simulator_state"] = (
            state["state"] if state else STATE_MAP.get(card["product_status"], "INELIGIBLE")
        )
        card["last_four"] = card.pop("product_number")[-4:]
        card.pop("customer_id", None)
        card["balance_semantics"] = (
            "team_fixture" if card["source_kind"] == "team_synthetic" else "historical_source_value"
        )
        return encode(card)

    def cards(self, user):
        with self.release() as (pg, release_id):
            ids = pg.execute(
                "SELECT product_id FROM bank.products WHERE release_id=%s AND customer_id=%s "
                "AND product_type=ANY(%s) UNION SELECT product_id FROM simulator.fixture_cards "
                "WHERE customer_id=%s ORDER BY product_id",
                (release_id, user["customer_id"], list(CARD_TYPES), user["customer_id"]),
            ).fetchall()
            return {
                "release_id": release_id,
                "mode": "test_simulator",
                "cards": [
                    self._card(pg, release_id, user["customer_id"], r["product_id"]) for r in ids
                ],
            }

    def card(self, user, product_id):
        with self.release() as (pg, release_id):
            return {
                "release_id": release_id,
                "mode": "test_simulator",
                "card": self._card(pg, release_id, user["customer_id"], product_id),
            }

    def movements(self, user, product_id, limit, before_date, cursor=None):
        continuation = MovementCursor.decode(cursor) if cursor is not None else None
        with self.release() as (pg, release_id):
            self._card(pg, release_id, user["customer_id"], product_id)
            seek = ""
            params = [release_id, user["customer_id"], product_id]
            if continuation is not None:
                if (continuation.customer_id, continuation.product_id) != (
                    user["customer_id"],
                    product_id,
                ):
                    raise HTTPException(422)
                if continuation.release_id != release_id:
                    # Never silently mix releases while continuing a historical page.
                    raise HTTPException(409)
                if before_date is not None and before_date != continuation.before_date:
                    raise HTTPException(422)
                before_date = continuation.before_date
                seek = (
                    "AND (process_date<%s OR (process_date=%s AND transaction_date<%s) "
                    "OR (process_date=%s AND transaction_date=%s AND transaction_id>%s)) "
                )
            params.extend([before_date, before_date])
            if continuation is not None:
                params.extend(
                    [
                        continuation.process_date,
                        continuation.process_date,
                        continuation.transaction_date,
                        continuation.process_date,
                        continuation.transaction_date,
                        continuation.transaction_id,
                    ]
                )
            params.append(limit + 1)
            rows = pg.execute(
                "SELECT transaction_id,transaction_date,process_date,amount,currency,"
                "transaction_type,transaction_status,merchant_name FROM bank.transactions "
                "WHERE release_id=%s AND customer_id=%s AND product_id=%s "
                "AND (%s::date IS NULL OR process_date<%s::date) "
                + seek
                + "ORDER BY process_date DESC,transaction_date DESC,transaction_id LIMIT %s",
                params,
            ).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = None
            if has_more:
                last = rows[-1]
                next_cursor = MovementCursor(
                    release_id=release_id,
                    customer_id=user["customer_id"],
                    product_id=product_id,
                    before_date=before_date,
                    process_date=last["process_date"],
                    transaction_date=last["transaction_date"],
                    transaction_id=last["transaction_id"],
                ).encode()
            return {
                "release_id": release_id,
                "semantics": "historical_source_movements",
                "movements": encode(rows),
                "next_cursor": next_cursor,
            }

    def action(self, user, product_id, action, idem_key, transaction_id=None, process_date=None):
        payload = {
            "product_id": product_id,
            "action": action,
            "transaction_id": transaction_id,
            "process_date": process_date.isoformat() if process_date else None,
        }
        with self.connect() as pg:
            # Serialize a customer's idempotency keys and actions; no split check/write race.
            pg.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,7236148203))",
                (user["customer_id"],),
            )
            previous = pg.execute(
                "SELECT payload,result FROM simulator.actions WHERE customer_id=%s "
                "AND idempotency_key=%s",
                (user["customer_id"], idem_key),
            ).fetchone()
            if previous:
                if previous["payload"] != payload:
                    raise HTTPException(409)
                return previous["result"]
            release_id = self._current(pg)
            card = self._card(pg, release_id, user["customer_id"], product_id)
            state = card["simulator_state"]
            if action == "unrecognized-charge":
                if not transaction_id or not process_date:
                    raise HTTPException(422)
                tx = pg.execute(
                    "SELECT transaction_id FROM bank.transactions WHERE release_id=%s AND "
                    "customer_id=%s AND product_id=%s AND transaction_id=%s AND process_date=%s",
                    (release_id, user["customer_id"], product_id, transaction_id, process_date),
                ).fetchone()
                if not tx:
                    raise HTTPException(404)
                outcome = "request_registered_for_human_review"
            elif action == "replacement":
                if state == "CLOSED":
                    raise HTTPException(409)
                outcome = "replacement_request_registered"
            else:
                try:
                    state = next_state(state, action)
                except ValueError:
                    raise HTTPException(409) from None
                pg.execute(
                    "INSERT INTO simulator.card_states(product_id,customer_id,state) "
                    "VALUES(%s,%s,%s) "
                    "ON CONFLICT(product_id) DO UPDATE SET state=excluded.state,updated_at=now() "
                    "WHERE simulator.card_states.customer_id=excluded.customer_id",
                    (product_id, user["customer_id"], state),
                )
                verified = pg.execute(
                    "SELECT state FROM simulator.card_states WHERE product_id=%s "
                    "AND customer_id=%s",
                    (product_id, user["customer_id"]),
                ).fetchone()
                if not verified or verified["state"] != state:
                    raise RuntimeError("action_verification_failed")
                outcome = "state_change_verified"
            result = {
                "action_id": uuid4().hex,
                "product_id": product_id,
                "action": action,
                "status": "succeeded",
                "outcome": outcome,
                "simulator_state": state,
                "simulated": True,
                "source_kind": card["source_kind"],
                "release_id": release_id,
            }
            if action in ("replacement", "unrecognized-charge"):
                result["request_id"] = uuid4().hex
            pg.execute(
                "INSERT INTO simulator.actions(action_id,customer_id,idempotency_key,"
                "payload,result) "
                "VALUES(%s,%s,%s,%s,%s)",
                (result["action_id"], user["customer_id"], idem_key, Jsonb(payload), Jsonb(result)),
            )
            return result

    def handoff(self, user, limit=20):
        with self.connect() as pg:
            rows = pg.execute(
                "SELECT result,created_at FROM simulator.actions WHERE customer_id=%s "
                "ORDER BY created_at DESC,action_id DESC LIMIT %s",
                (user["customer_id"], limit),
            ).fetchall()
            return {
                "mode": "test_simulator",
                "verified_action_results": encode(rows),
                "limitations": [
                    "Only committed simulated tool results are included",
                    "No refund, issuance, shipping or fraud decision is asserted",
                ],
            }

    def etl_status(self):
        with self.connect() as pg:
            release = pg.execute(
                "SELECT r.release_id,r.published_at,r.manifest->'tables' AS counts,"
                "r.manifest->'ml' AS ml,r.manifest->>'source_as_of' AS source_as_of "
                "FROM bank.current_release c JOIN bank.releases r USING(release_id) WHERE singleton"
            ).fetchone()
            run = pg.execute(
                "SELECT run_id,started_at,finished_at,status,error_code,phase FROM bank.etl_runs "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            return encode({"accepted_release": release, "latest_run": run})
