"""Simulator agent credentials and sessions, separate from customer authentication."""

import secrets
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from factored_bck.security import (
    DUMMY_PASSWORD_HASH,
    password_hash,
    password_matches,
    token_digest,
)


class AgentAuth:
    def __init__(self, store):
        self.store = store

    def _eligible(self, pg, agent_id, *, pin=False):
        return (
            pg.execute(
                "SELECT agent_id FROM bank.service_agents WHERE release_id=%s AND agent_id=%s "
                "AND agent_status='Active' AND agent_type IN ('Digital','Hybrid')",
                (self.store._current(pg, pin=pin), agent_id),
            ).fetchone()
            is not None
        )

    def provision(self, username, agent_id, password):
        if not 1 <= len(username) <= 100 or not 12 <= len(password) <= 200:
            raise ValueError("invalid_agent_credentials")
        with self.store.connect() as pg:
            if not self._eligible(pg, agent_id, pin=True):
                raise ValueError("agent_not_eligible_in_accepted_release")
            pg.execute(
                "INSERT INTO simulator.agent_users(username,password_hash,agent_id) "
                "VALUES(%s,%s,%s)",
                (username, password_hash(password), agent_id),
            )

    def login(self, username, password, peer):
        with self.store.connect() as pg:
            subject = self.store._admit_login(pg, username, peer, namespace="agent")
            account = pg.execute(
                "SELECT password_hash,agent_id,enabled FROM simulator.agent_users WHERE "
                "username=%s FOR UPDATE",
                (username,),
            ).fetchone()
            matched = password_matches(
                password, account["password_hash"] if account is not None else DUMMY_PASSWORD_HASH
            )
            valid = account is not None and account["enabled"] and matched
            if not valid:
                pg.commit()
                raise HTTPException(401)
            if not self._eligible(pg, account["agent_id"], pin=True):
                pg.commit()
                raise HTTPException(401)
            token = secrets.token_urlsafe(32)
            expiry = datetime.now(UTC) + timedelta(seconds=self.store.settings.session_seconds)
            pg.execute(
                "INSERT INTO simulator.agent_sessions VALUES(%s,%s,%s)",
                (token_digest(token), username, expiry),
            )
            pg.execute("DELETE FROM simulator.login_attempts WHERE subject_hash=%s", (subject,))
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_at": expiry.isoformat(),
            "mode": "agent_test_simulator",
        }

    def session(self, token, *, connection=None, lock=False):
        if not isinstance(token, str) or not 1 <= len(token) <= 200:
            raise HTTPException(401)
        with nullcontext(connection) if connection is not None else self.store.connect() as pg:
            account = pg.execute(
                "SELECT u.agent_id FROM simulator.agent_sessions s "
                "JOIN simulator.agent_users u USING(username) "
                "WHERE token_hash=%s AND expires_at>clock_timestamp() AND u.enabled"
                + (" FOR SHARE OF s,u" if lock else ""),
                (token_digest(token),),
            ).fetchone()
            if not account or not self._eligible(pg, account["agent_id"]):
                raise HTTPException(401)
        return account

    def logout(self, token):
        self.session(token)
        with self.store.connect() as pg:
            pg.execute(
                "DELETE FROM simulator.agent_sessions WHERE token_hash=%s", (token_digest(token),)
            )
        return {"status": "logged_out"}
