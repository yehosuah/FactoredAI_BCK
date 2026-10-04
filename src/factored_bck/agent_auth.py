"""Simulator agent credentials and sessions, separate from customer authentication."""

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from factored_bck.security import password_hash, password_matches, token_digest


class AgentAuth:
    def __init__(self, store):
        self.store = store

    def _eligible(self, pg, agent_id):
        return (
            pg.execute(
                "SELECT agent_id FROM bank.service_agents WHERE release_id=%s AND agent_id=%s "
                "AND agent_status='Active' AND agent_type IN ('Digital','Hybrid')",
                (self.store._current(pg), agent_id),
            ).fetchone()
            is not None
        )

    def provision(self, username, agent_id, password):
        if not 1 <= len(username) <= 100 or not 12 <= len(password) <= 200:
            raise ValueError("invalid_agent_credentials")
        with self.store.connect() as pg:
            if not self._eligible(pg, agent_id):
                raise ValueError("agent_not_eligible_in_accepted_release")
            pg.execute(
                "INSERT INTO simulator.agent_users(username,password_hash,agent_id) "
                "VALUES(%s,%s,%s)",
                (username, password_hash(password), agent_id),
            )

    def login(self, username, password, peer):
        subject = token_digest(username + "|" + peer)
        with self.store.connect() as pg:
            attempt = pg.execute(
                "INSERT INTO simulator.agent_login_attempts VALUES(%s,1,now()) "
                "ON CONFLICT(subject_hash) DO UPDATE SET attempts=CASE WHEN "
                "simulator.agent_login_attempts.window_start<now()-interval '5 minutes' "
                "THEN 1 ELSE simulator.agent_login_attempts.attempts+1 END, window_start=CASE "
                "WHEN simulator.agent_login_attempts.window_start<now()-interval '5 minutes' "
                "THEN now() ELSE simulator.agent_login_attempts.window_start END "
                "RETURNING attempts",
                (subject,),
            ).fetchone()
            account = pg.execute(
                "SELECT password_hash,agent_id,enabled FROM simulator.agent_users WHERE "
                "username=%s",
                (username,),
            ).fetchone()
            allowed = attempt["attempts"] <= 10
            valid = (
                account
                and account["enabled"]
                and password_matches(password, account["password_hash"])
            )
            if not allowed or not valid:
                pg.commit()
                raise HTTPException(429 if not allowed else 401)
            if not self._eligible(pg, account["agent_id"]):
                pg.commit()
                raise HTTPException(401)
            token = secrets.token_urlsafe(32)
            expiry = datetime.now(UTC) + timedelta(seconds=self.store.settings.session_seconds)
            pg.execute(
                "INSERT INTO simulator.agent_sessions VALUES(%s,%s,%s)",
                (token_digest(token), username, expiry),
            )
            pg.execute(
                "DELETE FROM simulator.agent_login_attempts WHERE subject_hash=%s", (subject,)
            )
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_at": expiry.isoformat(),
            "mode": "agent_test_simulator",
        }

    def session(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 200:
            raise HTTPException(401)
        with self.store.connect() as pg:
            account = pg.execute(
                "SELECT u.agent_id FROM simulator.agent_sessions s "
                "JOIN simulator.agent_users u USING(username) "
                "WHERE token_hash=%s AND expires_at>now() AND u.enabled",
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
