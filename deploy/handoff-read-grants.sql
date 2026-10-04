-- Run as a privileged administrator AFTER the ETL creates bank tables.
-- A separate inherited NOLOGIN role survives the current ETL loader's revocations
-- of privileges granted directly to backend_api. Never grant table-wide SELECT.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='backend_handoff_reader') THEN
        CREATE ROLE backend_handoff_reader NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA bank TO backend_handoff_reader;
GRANT SELECT (release_id, customer_id, segment)
    ON bank.customers TO backend_handoff_reader;
GRANT SELECT (release_id, agent_id, agent_type, experience_level, languages,
              specialty, avg_csat, agent_status)
    ON bank.service_agents TO backend_handoff_reader;
GRANT backend_handoff_reader TO backend_api;
