"""Authenticated persistent handoffs; one transaction commits routing and safe evidence."""

import os
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from factored_bck.agent_auth import AgentAuth
from factored_bck.evidence import ActionEvidence
from factored_bck.handoff import (
    LEVELS,
    LIMITATIONS,
    REASONS,
    SEVERITIES,
    CreateHandoffArguments,
    Triage,
    route,
)
from factored_bck.store import encode


class HandoffStore:
    def __init__(self, store):
        self.store = store
        self.agents = AgentAuth(store)

    def _customer(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 200:
            raise HTTPException(401)
        return self.store.session(token)["customer_id"]

    def _routing(self, pg, release_id, customer_id, triage, *, only_agent_id=None):
        customer = pg.execute(
            "SELECT segment FROM bank.customers WHERE release_id=%s AND customer_id=%s",
            (release_id, customer_id),
        ).fetchone()
        # Accounts are administratively provisioned; snapshot presence alone is not delivery.
        agents = pg.execute(
            "SELECT a.agent_id,a.agent_type,a.experience_level,a.languages,a.specialty,"
            "a.avg_csat,a.agent_status FROM bank.service_agents a "
            "JOIN simulator.agent_users u ON u.agent_id=a.agent_id AND u.enabled "
            "WHERE a.release_id=%s AND (%s::text IS NULL OR a.agent_id=%s) FOR SHARE OF u",
            (release_id, only_agent_id, only_agent_id),
        ).fetchall()
        result = route(
            triage,
            customer["segment"] if customer else None,
            agents,
            self.store.settings.critical_senior_fallback_reasons,
        )
        return {**result, "release_id": release_id}

    def _evidence(self, pg, release_id, customer_id, product_id):
        card = self.store._card(pg, release_id, customer_id, product_id) if product_id else None
        rows = pg.execute(
            "SELECT result,created_at FROM simulator.actions WHERE customer_id=%s "
            "AND (%s::text IS NULL OR result->>'product_id'=%s) "
            "ORDER BY created_at DESC,action_id DESC LIMIT 21",
            (customer_id, product_id, product_id),
        ).fetchall()
        evidence = []
        for row in rows[:20]:
            receipt = ActionEvidence.model_validate(row["result"])
            if not receipt.simulated:
                raise RuntimeError("invalid_simulator_evidence")
            evidence.append(
                {
                    "receipt": receipt.model_dump(exclude_none=True),
                    "committed_at": row["created_at"].isoformat(),
                }
            )
        return {
            "source": "committed_simulator_state",
            "release_id": release_id,
            "actions": evidence,
            "actions_truncated": len(rows) > 20,
            "card": {k: card[k] for k in ("product_id", "simulator_state", "source_kind")}
            if card
            else None,
        }

    @staticmethod
    def _resource(row):
        # No customer identity, account name, idempotency payload or credential leaves this module.
        return encode(
            {
                **{
                    key: row[key]
                    for key in (
                        "handoff_id",
                        "status",
                        "reason",
                        "severity",
                        "required_level",
                        "release_id",
                        "assigned_agent_id",
                        "created_at",
                        "updated_at",
                        "assigned_at",
                        "accepted_at",
                        "resolved_at",
                        "cancelled_at",
                        "model_context",
                        "verified_evidence",
                        "routing",
                    )
                },
                "triage": {
                    k: row["request_payload"][k]
                    for k in (
                        "reason",
                        "severity",
                        "required_specialty",
                        "minimum_experience",
                        "language",
                        "product_id",
                    )
                },
                "assignment_status": row["routing"]["assignment_status"],
                "queue": row["routing"]["queue"],
                "manual_routing_required": row["routing"]["manual_routing_required"]
                and row["status"] == "queued",
                "service_priority": row["routing"]["service_priority"],
                "persisted": True,
                "limitations": LIMITATIONS,
            }
        )

    def create(self, token, arguments: CreateHandoffArguments):
        user = self.store.session(token)
        arguments = CreateHandoffArguments.model_validate(arguments)
        # Authentication is transport context, never content or an idempotency key.
        if token in arguments.model_dump_json():
            raise HTTPException(422)
        triage = arguments.triage
        payload = triage.model_dump(mode="json")
        with self.store.connect() as pg:
            pg.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,7236148203))",
                (user["customer_id"],),
            )
            current_user = self.store.session(token, connection=pg, lock=True)
            if current_user["customer_id"] != user["customer_id"]:
                raise HTTPException(401)
            user = current_user
            previous = pg.execute(
                "SELECT * FROM simulator.handoffs WHERE customer_id=%s AND idempotency_key=%s",
                (user["customer_id"], arguments.idempotency_key),
            ).fetchone()
            if previous:
                if previous["request_payload"] != payload:
                    raise HTTPException(409)
                return self._resource(previous)
            release_id = self.store._current(pg, pin=True)
            evidence = self._evidence(pg, release_id, user["customer_id"], triage.product_id)
            routing = self._routing(pg, release_id, user["customer_id"], triage)
            assigned = routing["assigned_agent_id"] is not None
            row = pg.execute(
                "INSERT INTO simulator.handoffs(handoff_id,customer_id,created_by,idempotency_key,"
                "request_payload,release_id,reason,severity,required_level,assigned_agent_id,status,"
                "routing,model_context,verified_evidence,assigned_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s THEN now() END) "
                "RETURNING *",
                (
                    uuid4().hex,
                    user["customer_id"],
                    user["username"],
                    arguments.idempotency_key,
                    Jsonb(payload),
                    release_id,
                    triage.reason,
                    routing["severity"],
                    routing["required_level"],
                    routing["assigned_agent_id"],
                    "assigned" if assigned else "queued",
                    Jsonb(routing),
                    Jsonb(
                        {
                            "trust": "model_or_customer_provided",
                            "summary": triage.summary,
                            "context": triage.context,
                            "unresolved_questions": triage.unresolved_questions,
                        }
                    ),
                    Jsonb(evidence),
                    assigned,
                ),
            ).fetchone()
            result = self._resource(row)
        return result  # Only after the transaction commits, never a requested-intent receipt.

    def get(self, token, handoff_id):
        customer = self._customer(token)
        with self.store.connect() as pg:
            row = pg.execute(
                "SELECT * FROM simulator.handoffs WHERE handoff_id=%s AND customer_id=%s",
                (handoff_id, customer),
            ).fetchone()
        if not row:
            raise HTTPException(404)
        return self._resource(row)

    def list(self, token, limit=20, offset=0, *, agent=False):
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
            raise HTTPException(422)
        identity = self.agents.session(token)["agent_id"] if agent else self._customer(token)
        # Column comes only from the trusted method flag, never external SQL or tool arguments.
        column = "assigned_agent_id" if agent else "customer_id"
        with self.store.connect() as pg:
            rows = pg.execute(
                f"SELECT * FROM simulator.handoffs WHERE {column}=%s "
                "ORDER BY (routing->'service_priority'->>'severity_rank')::int DESC, "
                "(routing->'service_priority'->>'premium_uplift')::boolean DESC, "
                "created_at,handoff_id LIMIT %s OFFSET %s",
                (identity, limit + 1, offset),
            ).fetchall()
        return {
            "handoffs": [self._resource(r) for r in rows[:limit]],
            "next_offset": offset + limit
            if len(rows) > limit and offset + limit <= 10000
            else None,
        }

    def agent_get(self, token, handoff_id):
        agent_id = self.agents.session(token)["agent_id"]
        with self.store.connect() as pg:
            row = pg.execute(
                "SELECT * FROM simulator.handoffs WHERE handoff_id=%s AND assigned_agent_id=%s",
                (handoff_id, agent_id),
            ).fetchone()
        if not row:
            raise HTTPException(404)
        return self._resource(row)

    def transition(self, token, handoff_id, operation):
        if operation not in ("accept", "resolve", "cancel"):
            raise HTTPException(422)
        customer = self._customer(token) if operation == "cancel" else None
        agent = self.agents.session(token)["agent_id"] if operation != "cancel" else None
        column, identity = ("customer_id", customer) if customer else ("assigned_agent_id", agent)
        transitions = {
            "accept": (("assigned",), "accepted", "accepted_at"),
            "resolve": (("accepted",), "resolved", "resolved_at"),
            "cancel": (("queued", "assigned"), "cancelled", "cancelled_at"),
        }
        allowed, target, timestamp = transitions[operation]
        with self.store.connect() as pg:
            row = pg.execute(
                f"SELECT * FROM simulator.handoffs WHERE handoff_id=%s AND {column}=%s FOR UPDATE",
                (handoff_id, identity),
            ).fetchone()
            if not row:
                raise HTTPException(404)
            if operation == "cancel":
                current_identity = self.store.session(token, connection=pg, lock=True)[
                    "customer_id"
                ]
            else:
                current_identity = self.agents.session(token, connection=pg, lock=True)["agent_id"]
            if current_identity != identity:
                raise HTTPException(401)
            # Replay recovers committed evidence; it does not perform a new transition.
            # Current session/account/release membership was rechecked above.
            if row["status"] == target:
                return self._resource(row)
            if operation != "cancel":
                eligibility = self._routing(
                    pg,
                    self.store._current(pg, pin=True),
                    row["customer_id"],
                    Triage.model_validate(row["request_payload"]),
                    only_agent_id=agent,
                )
                if eligibility["assigned_agent_id"] is None:
                    raise HTTPException(409)
            if row["status"] not in allowed:
                raise HTTPException(409)
            row = pg.execute(
                f"UPDATE simulator.handoffs SET status=%s,{timestamp}=now(),updated_at=now() "
                "WHERE handoff_id=%s RETURNING *",
                (target, handoff_id),
            ).fetchone()
            result = self._resource(row)
        return result

    def reroute(self, handoff_id):
        """Local administrator operation for queued cases; cannot supply a target agent."""
        with self.store.connect() as pg:
            row = pg.execute(
                "SELECT * FROM simulator.handoffs WHERE handoff_id=%s FOR UPDATE", (handoff_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404)
            if row["status"] != "queued":
                raise HTTPException(409)
            routing = self._routing(
                pg,
                self.store._current(pg, pin=True),
                row["customer_id"],
                Triage.model_validate(row["request_payload"]),
            )
            assigned = routing["assigned_agent_id"] is not None
            updated = pg.execute(
                "UPDATE simulator.handoffs SET routing=%s,assigned_agent_id=%s,status=%s,"
                "assigned_at=CASE WHEN %s THEN now() END,updated_at=now() WHERE "
                "handoff_id=%s RETURNING *",
                (
                    Jsonb(routing),
                    routing["assigned_agent_id"],
                    "assigned" if assigned else "queued",
                    assigned,
                    handoff_id,
                ),
            ).fetchone()
        return self._resource(updated)

    def recover(self, handoff_id):
        """Local database administrator only; never register in HTTP/model transports.

        Recovery is permitted only when the previous agent fails the full current
        routing eligibility. The case lock serializes transitions/recovery, account
        locks protect enabled status through commit, and authority is derived from
        local/database identity rather than supplied by a caller.
        """
        with self.store.connect() as pg:
            row = pg.execute(
                "SELECT * FROM simulator.handoffs WHERE handoff_id=%s FOR UPDATE",
                (handoff_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404)
            if row["status"] not in ("assigned", "accepted"):
                raise HTTPException(409)
            release_id = self.store._current(pg, pin=True)
            triage = Triage.model_validate(row["request_payload"])
            pg.execute(
                "SELECT enabled FROM simulator.agent_users WHERE agent_id=%s FOR SHARE",
                (row["assigned_agent_id"],),
            )
            previous_agent = self._routing(
                pg,
                release_id,
                row["customer_id"],
                triage,
                only_agent_id=row["assigned_agent_id"],
            )
            if previous_agent["assigned_agent_id"] is not None:
                raise HTTPException(409)
            routing = self._routing(pg, release_id, row["customer_id"], triage)
            assigned = routing["assigned_agent_id"] is not None
            authority = pg.execute("SELECT session_user AS database_role").fetchone() | {
                "kind": "local_database_credentials",
                "effective_uid": os.geteuid(),
            }
            audit = pg.execute(
                "INSERT INTO simulator.handoff_recoveries(recovery_id,handoff_id,"
                "prior_status,prior_agent_id,prior_assigned_at,prior_accepted_at,prior_routing,"
                "status,assigned_agent_id,release_id,reason,authority) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'assigned_agent_unavailable',%s) "
                "RETURNING *",
                (
                    uuid4().hex,
                    handoff_id,
                    row["status"],
                    row["assigned_agent_id"],
                    row["assigned_at"],
                    row["accepted_at"],
                    Jsonb(row["routing"]),
                    "assigned" if assigned else "queued",
                    routing["assigned_agent_id"],
                    release_id,
                    Jsonb(authority),
                ),
            ).fetchone()
            updated = pg.execute(
                "UPDATE simulator.handoffs SET routing=%s,assigned_agent_id=%s,status=%s,"
                "assigned_at=CASE WHEN %s THEN clock_timestamp() END,accepted_at=NULL,"
                "updated_at=clock_timestamp() WHERE handoff_id=%s RETURNING *",
                (
                    Jsonb(routing),
                    routing["assigned_agent_id"],
                    "assigned" if assigned else "queued",
                    assigned,
                    handoff_id,
                ),
            ).fetchone()
            result = self._resource(updated) | {"recovery": encode(audit)}
        return result

    def metrics(self):
        with self.store.connect() as pg:
            pg.execute("SET TRANSACTION READ ONLY")
            pg.execute("SET LOCAL statement_timeout = '2s'")
            rows = pg.execute(
                "SELECT severity,reason,required_level,count(*) AS total, "
                "count(*) FILTER (WHERE assigned_agent_id IS NOT NULL) AS assigned, "
                "count(*) FILTER (WHERE routing->>'fallback_used'='true') AS fallbacks, "
                "count(*) FILTER (WHERE status='queued' AND "
                "routing->>'queue'='critical_review') AS critical_review "
                "FROM simulator.handoffs GROUP BY 1,2,3"
            ).fetchall()
        total, assigned = sum(r["total"] for r in rows), sum(r["assigned"] for r in rows)
        return {
            "status": "available",
            "scope": "all_customers",
            "total_handoffs": total,
            "assigned": assigned,
            "unassigned": total - assigned,
            "fallback_assignments": sum(r["fallbacks"] for r in rows),
            "critical_review": sum(r["critical_review"] for r in rows),
            "by_severity": {
                v: sum(r["total"] for r in rows if r["severity"] == v) for v in SEVERITIES
            },
            "by_reason": {v: sum(r["total"] for r in rows if r["reason"] == v) for v in REASONS},
            "by_required_level": {
                v: sum(r["total"] for r in rows if r["required_level"] == v) for v in LEVELS
            },
            "semantics": "All retained cases; assigned includes terminal cases and does "
            "not mean resolved",
        }

    def check_configuration(self):
        """Deployment preflight: verify every consumed source column without reading PII."""
        with self.store.connect() as pg:
            self.store._current(pg)
            pg.execute("SELECT release_id,customer_id,segment FROM bank.customers LIMIT 0")
            pg.execute(
                "SELECT release_id,agent_id,agent_type,experience_level,languages,specialty,"
                "avg_csat,agent_status FROM bank.service_agents LIMIT 0"
            )
            pg.execute("SELECT handoff_id FROM simulator.handoffs LIMIT 0")
