"""Additive simulator DDL, executed under Store's startup advisory lock."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS simulator.agent_users (
    username text PRIMARY KEY,
    password_hash text NOT NULL,
    agent_id text NOT NULL UNIQUE,
    enabled boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS simulator.agent_sessions (
    token_hash text PRIMARY KEY,
    username text NOT NULL REFERENCES simulator.agent_users(username),
    expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS simulator.agent_login_attempts (
    subject_hash text PRIMARY KEY, attempts integer NOT NULL, window_start timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS simulator.handoffs (
    handoff_id text PRIMARY KEY,
    customer_id text NOT NULL,
    created_by text NOT NULL,
    idempotency_key text NOT NULL,
    request_payload jsonb NOT NULL,
    release_id text NOT NULL,
    reason text NOT NULL CHECK (reason IN
        ('fraud','complaint','technical_support','card_activation','replacement','card_support','other')),
    severity text NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    required_level text NOT NULL CHECK (required_level IN
        ('Junior','Mid-Senior','Senior','Specialist')),
    assigned_agent_id text REFERENCES simulator.agent_users(agent_id),
    status text NOT NULL CHECK (status IN ('queued','assigned','accepted','resolved','cancelled')),
    routing jsonb NOT NULL,
    model_context jsonb NOT NULL,
    verified_evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    assigned_at timestamptz,
    accepted_at timestamptz,
    resolved_at timestamptz,
    cancelled_at timestamptz,
    UNIQUE(customer_id, idempotency_key),
    CHECK ((status = 'queued' AND assigned_agent_id IS NULL)
        OR (status IN ('assigned','accepted','resolved') AND assigned_agent_id IS NOT NULL)
        OR status = 'cancelled'),
    CHECK (status NOT IN ('accepted','resolved') OR accepted_at IS NOT NULL),
    CHECK (status <> 'resolved' OR resolved_at IS NOT NULL),
    CHECK (status <> 'cancelled' OR cancelled_at IS NOT NULL)
);
ALTER TABLE simulator.handoffs ADD COLUMN IF NOT EXISTS recovery_history jsonb NOT NULL
    DEFAULT '[]'::jsonb CHECK (jsonb_typeof(recovery_history) = 'array');
CREATE INDEX IF NOT EXISTS handoffs_customer ON simulator.handoffs(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS handoffs_agent ON simulator.handoffs(assigned_agent_id, created_at DESC);
"""
