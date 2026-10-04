"""Customer-scoped historical reads and transactional, persistent simulated actions."""

import secrets
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from factored_bck.confirmation_schema import SCHEMA as CONFIRMATION_SCHEMA
from factored_bck.handoff_schema import SCHEMA as HANDOFF_SCHEMA
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
# Accepted ETL publisher takes this exclusive xact lock before touching bank.
# Backend mutations take its shared form; no UPDATE grant on the pointer is needed.
ETL_PUBLISH_LOCK = 7236148201
LOGIN_KDF_LOCK = 7236148203
LOGIN_PEER_LIMIT = 30
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

            pg.execute(HANDOFF_SCHEMA)
            pg.execute(CONFIRMATION_SCHEMA)

    def ready(self):
        with self.connect() as pg:
            return {"release_id": self._current(pg)}

    def _current(self, pg, *, pin=False):
        if pin:
            admitted = pg.execute(
                "SELECT pg_try_advisory_xact_lock_shared(%s) AS admitted", (ETL_PUBLISH_LOCK,)
            ).fetchone()["admitted"]
            if not admitted:
                # Fail closed rather than hold request/customer locks through a long ETL run.
                raise HTTPException(503, headers={"Retry-After": "1"})
        row = pg.execute(
            "SELECT r.release_id,r.manifest->>'contract_version' AS contract_version "
            "FROM bank.current_release c JOIN bank.releases r USING(release_id) WHERE singleton"
        ).fetchone()
        if not row or row["contract_version"] != SOURCE_CONTRACT_VERSION:
            raise HTTPException(503)
        return row["release_id"]

    @staticmethod
    def _login_attempt(pg, subject):
        return pg.execute(
            "INSERT INTO simulator.login_attempts VALUES(%s,1,now()) "
            "ON CONFLICT(subject_hash) DO UPDATE SET attempts=CASE WHEN "
            "simulator.login_attempts.window_start<now()-interval '5 minutes' "
            "THEN 1 ELSE simulator.login_attempts.attempts+1 END, window_start=CASE "
            "WHEN simulator.login_attempts.window_start<now()-interval '5 minutes' "
            "THEN now() ELSE simulator.login_attempts.window_start END RETURNING attempts",
            (subject,),
        ).fetchone()["attempts"]

    def _admit_login(self, pg, username, peer, *, namespace="user"):
        """One password-work boundary shared by customer and agent transports."""
        admitted = pg.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS admitted", (LOGIN_KDF_LOCK,)
        ).fetchone()["admitted"]
        if not admitted:
            raise HTTPException(429, headers={"Retry-After": "1"})
        peer_attempts = self._login_attempt(pg, token_digest("login:peer:" + peer))
        if peer_attempts > LOGIN_PEER_LIMIT:
            pg.commit()
            raise HTTPException(429)
        subject = token_digest("login:" + namespace + ":" + username + "|" + peer)
        if self._login_attempt(pg, subject) > 10:
            pg.commit()
            raise HTTPException(429)
        return subject

    def login(self, username, password, peer):
        with self.connect() as pg:
            subject = self._admit_login(pg, username, peer)
            user = pg.execute(
                "SELECT * FROM simulator.users WHERE username=%s FOR UPDATE", (username,)
            ).fetchone()
            # Admitted attempts perform scrypt, including unknown usernames. The user lock keeps
            # session issuance atomic with credential rotation and session revocation.
            matched = password_matches(
                password, user["password_hash"] if user is not None else DUMMY_PASSWORD_HASH
            )
            valid = user is not None and matched
            if not valid:
                pg.commit()  # Failed attempts must persist before raising an HTTP exception.
                raise HTTPException(401)
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

    def session(self, token, *, connection=None, lock=False):
        with nullcontext(connection) if connection is not None else self.connect() as pg:
            user = pg.execute(
                "SELECT u.username,u.customer_id,u.source_kind FROM simulator.sessions s "
                "JOIN simulator.users u USING(username) WHERE token_hash=%s "
                "AND expires_at>clock_timestamp()" + (" FOR SHARE OF s,u" if lock else ""),
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
            if continuation is not None:
                if (continuation.customer_id, continuation.product_id) != (
                    user["customer_id"],
                    product_id,
                ):
                    raise HTTPException(422)
                if continuation.release_id != release_id:
                    # A scoped stale cursor conflicts even if its card was removed.
                    raise HTTPException(409)
            self._card(pg, release_id, user["customer_id"], product_id)
            seek = ""
            params = [release_id, user["customer_id"], product_id]
            if continuation is not None:
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

    def preview_action(
        self, pg, release_id, user, product_id, action, transaction_id=None, process_date=None
    ):
        """Owned action eligibility without writes; execution uses this same implementation."""
        if action not in (
            "block",
            "pause",
            "reactivate",
            "activate",
            "replacement",
            "unrecognized-charge",
        ):
            raise HTTPException(422)
        if action != "unrecognized-charge" and (
            transaction_id is not None or process_date is not None
        ):
            raise HTTPException(422)
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
            outcome = "state_change_verified"
        return card, state, outcome

    def action(
        self,
        user,
        product_id,
        action,
        idem_key,
        transaction_id=None,
        process_date=None,
        *,
        connection=None,
        release_id=None,
    ):
        """Trusted backend execution. With connection, the caller owns the atomic commit."""
        payload = {
            "product_id": product_id,
            "action": action,
            "transaction_id": transaction_id,
            "process_date": process_date.isoformat() if process_date else None,
        }
        with nullcontext(connection) if connection is not None else self.connect() as pg:
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
            current_release = self._current(pg, pin=True)
            if release_id is not None and release_id != current_release:
                raise HTTPException(409)
            release_id = current_release
            card, state, outcome = self.preview_action(
                pg, release_id, user, product_id, action, transaction_id, process_date
            )
            if outcome == "state_change_verified":
                pg.execute(
                    "INSERT INTO simulator.card_states(product_id,customer_id,state) "
                    "VALUES(%s,%s,%s) "
                    "ON CONFLICT(product_id) DO UPDATE SET state=excluded.state,updated_at=now(), "
                    "revision=simulator.card_states.revision+1 "
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

    def action_metrics(self):
        """Aggregate committed evidence only; never load payloads or identifying fields."""
        with self.connect() as pg:
            pg.execute("SET TRANSACTION READ ONLY")
            pg.execute("SET LOCAL statement_timeout = '2s'")
            rows = pg.execute(
                "SELECT CASE WHEN result->>'action'=ANY(%s) THEN result->>'action' "
                "ELSE 'other' END AS action, "
                "CASE WHEN result->>'outcome'=ANY(%s) THEN result->>'outcome' "
                "ELSE 'other' END AS outcome, count(*) AS committed_count, "
                "count(*) FILTER (WHERE result->>'status'='succeeded') AS succeeded_count "
                "FROM simulator.actions GROUP BY 1,2 ORDER BY 1,2",
                (
                    [
                        "block",
                        "pause",
                        "reactivate",
                        "activate",
                        "replacement",
                        "unrecognized-charge",
                    ],
                    [
                        "state_change_verified",
                        "replacement_request_registered",
                        "request_registered_for_human_review",
                    ],
                ),
            ).fetchall()
        return {
            "status": "available",
            "scope": "all_customers",
            "source": "simulator.actions",
            "window": {
                "kind": "all_retained_committed_rows",
                "observed_at": datetime.now(UTC).isoformat(),
            },
            "total_committed": sum(row["committed_count"] for row in rows),
            "total_succeeded": sum(row["succeeded_count"] for row in rows),
            "total_failures": None,
            "actions": rows,
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
