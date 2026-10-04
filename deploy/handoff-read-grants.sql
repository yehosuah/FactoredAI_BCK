-- Run as a privileged administrator AFTER the ETL creates bank tables.
-- Set factored_bck.backend_role to the exact configured BCK_DB_USER in this session.
-- A separate inherited NOLOGIN role survives revocations of direct backend grants.
-- Validate both roles before granting any restricted column; never table-wide SELECT.
DO $$
DECLARE
    backend_role text := current_setting('factored_bck.backend_role', true);
BEGIN
    IF backend_role IS NULL OR backend_role = '' THEN
        RAISE EXCEPTION 'backend_role_required';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname=backend_role AND rolcanlogin) THEN
        RAISE EXCEPTION 'backend_role_invalid';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname='backend_handoff_reader' AND rolcanlogin) THEN
        RAISE EXCEPTION 'reader_must_be_nologin';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='backend_handoff_reader') THEN
        CREATE ROLE backend_handoff_reader NOLOGIN;
    END IF;
    GRANT USAGE ON SCHEMA bank TO backend_handoff_reader;
    GRANT SELECT (release_id, customer_id, segment)
        ON bank.customers TO backend_handoff_reader;
    GRANT SELECT (release_id, agent_id, agent_type, experience_level, languages,
                  specialty, avg_csat, agent_status)
        ON bank.service_agents TO backend_handoff_reader;
    EXECUTE format('GRANT backend_handoff_reader TO %I', backend_role);
END $$;
