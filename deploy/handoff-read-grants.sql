-- Run as a privileged administrator AFTER the ETL creates bank tables.
-- Set factored_bck.backend_role to the exact configured BCK_DB_USER in this session.
-- A separate inherited NOLOGIN role survives revocations of direct backend grants.
-- Validate both roles before granting any restricted column; never table-wide SELECT.
DO $$
DECLARE
    backend_role text := current_setting('factored_bck.backend_role', true);
    reader oid;
    database_id oid;
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
    SELECT oid INTO reader FROM pg_roles WHERE rolname='backend_handoff_reader';
    SELECT oid INTO database_id FROM pg_database WHERE datname=current_database();
    IF reader IS NOT NULL THEN
        -- Adding columns must not expose them to an existing unrelated member,
        -- including an intermediary NOLOGIN role or delegated membership admin.
        IF EXISTS (
            SELECT FROM pg_auth_members WHERE roleid=reader
            AND (member<>(SELECT oid FROM pg_roles WHERE rolname=backend_role) OR admin_option)
        ) THEN
            RAISE EXCEPTION 'reader_has_unexpected_members';
        END IF;
        -- Reject attributes and parent roles, even when inheritance is currently disabled.
        IF EXISTS (
            SELECT FROM pg_roles WHERE oid=reader
            AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls)
        ) OR EXISTS (SELECT FROM pg_auth_members WHERE member=reader) THEN
            RAISE EXCEPTION 'reader_has_excess_privileges';
        END IF;
        -- pg_shdepend is cluster-wide: reject ownership, defaults, policies, privileges
        -- in other databases and every object outside this database's narrow allowlist.
        IF EXISTS (
            SELECT FROM pg_shdepend
            WHERE refclassid='pg_authid'::regclass AND refobjid=reader
            AND NOT (
                deptype='a' AND dbid=database_id AND (
                    (classid='pg_namespace'::regclass AND objid=to_regnamespace('bank'))
                    OR (classid='pg_class'::regclass AND objid IN (
                        to_regclass('bank.customers'),to_regclass('bank.service_agents')
                    ))
                )
            )
        ) THEN
            RAISE EXCEPTION 'reader_has_excess_privileges';
        END IF;
        -- Allowed objects still need exact ACL validation: no CREATE, table-wide
        -- permissions, contact columns, writes or grant options may be inherited.
        IF EXISTS (
            SELECT FROM pg_namespace n, LATERAL aclexplode(n.nspacl) acl
            WHERE n.oid=to_regnamespace('bank') AND acl.grantee=reader
            AND (acl.privilege_type<>'USAGE' OR acl.is_grantable)
        ) OR EXISTS (
            SELECT FROM pg_class c, LATERAL aclexplode(c.relacl) acl
            WHERE c.oid IN (to_regclass('bank.customers'),to_regclass('bank.service_agents'))
            AND acl.grantee=reader
        ) OR EXISTS (
            SELECT FROM pg_attribute a, LATERAL aclexplode(a.attacl) acl
            WHERE a.attrelid IN (to_regclass('bank.customers'),to_regclass('bank.service_agents'))
            AND acl.grantee=reader AND (
                acl.privilege_type<>'SELECT' OR acl.is_grantable OR NOT (
                    (a.attrelid=to_regclass('bank.customers')
                     AND a.attname IN ('release_id','customer_id','segment'))
                    OR (a.attrelid=to_regclass('bank.service_agents')
                     AND a.attname IN ('release_id','agent_id','agent_type','experience_level',
                                      'languages','specialty','avg_csat','agent_status'))
                )
            )
        ) THEN
            RAISE EXCEPTION 'reader_has_excess_privileges';
        END IF;
    END IF;
    IF reader IS NULL THEN
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
