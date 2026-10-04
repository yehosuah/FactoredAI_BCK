"""Additive confirmation migration, run under Store's existing startup lock."""

SCHEMA = """
ALTER TABLE simulator.card_states ADD COLUMN IF NOT EXISTS revision bigint NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS simulator.action_confirmations (
    confirmation_id text PRIMARY KEY,
    customer_id text NOT NULL,
    preparation_key text NOT NULL,
    command_key text NOT NULL UNIQUE,
    command jsonb NOT NULL,
    prepared_state jsonb NOT NULL,
    conversation_id text,
    policy_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('pending','executed','cancelled','expired','stale')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    finished_at timestamptz,
    action_id text UNIQUE REFERENCES simulator.actions(action_id),
    evidence jsonb,
    UNIQUE(customer_id,preparation_key),
    CHECK (expires_at > created_at),
    CHECK ((status='pending' AND finished_at IS NULL)
        OR (status<>'pending' AND finished_at IS NOT NULL)),
    CHECK ((status='executed' AND action_id IS NOT NULL AND evidence IS NOT NULL)
        OR (status<>'executed' AND action_id IS NULL AND evidence IS NULL))
);
CREATE INDEX IF NOT EXISTS confirmations_customer
    ON simulator.action_confirmations(customer_id,created_at DESC);
"""
