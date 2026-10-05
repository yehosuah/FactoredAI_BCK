"""Additive P04 persistence, applied under the backend startup advisory lock."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS simulator.conversations (
    conversation_id text PRIMARY KEY,
    customer_id text NOT NULL,
    creation_key text NOT NULL,
    initial_language text NOT NULL CHECK (initial_language IN ('es','pt')),
    language text NOT NULL CHECK (language IN ('es','pt')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(customer_id,creation_key)
);
CREATE TABLE IF NOT EXISTS simulator.conversation_turns (
    turn_id text PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES simulator.conversations(conversation_id),
    idempotency_key text NOT NULL,
    payload jsonb NOT NULL,
    language text NOT NULL CHECK (language IN ('es','pt')),
    adapter jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(conversation_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS simulator.conversation_events (
    event_id text PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES simulator.conversations(conversation_id),
    turn_id text NOT NULL REFERENCES simulator.conversation_turns(turn_id),
    sequence bigint NOT NULL CHECK (sequence>0),
    kind text NOT NULL CHECK (kind IN ('user_message','answer','clarification','tool_result',
        'confirmation_prepared','confirmation_status','handoff_created','handoff_status','error')),
    trust text NOT NULL CHECK (trust IN ('untrusted','backend')),
    data jsonb NOT NULL,
    confirmation_id text REFERENCES simulator.action_confirmations(confirmation_id),
    handoff_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(conversation_id,sequence)
);
CREATE INDEX IF NOT EXISTS conversation_turns_order
    ON simulator.conversation_turns(conversation_id,created_at,turn_id);
"""
