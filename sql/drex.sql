-- Drex for PostgreSQL: the PL/Python functions (self-hosted Postgres).
--
-- Needs the plpython3u extension and the drex-postgres Python package installed into the Python that plpython3u
-- runs (see README.md). Run as a superuser, with the schema the functions go in first on search_path:
--
--   psql -v ON_ERROR_STOP=1 -c 'SET search_path = public' -f sql/drex.sql
--
-- The API key is NACE_API_KEY in the server's environment, and NACE_BASE_URL optionally names another endpoint.
-- Every function is revoked from PUBLIC: GRANT EXECUTE to the roles that may spend the key.

BEGIN;

CREATE EXTENSION IF NOT EXISTS plpython3u;

-- Fails the install now, not the first query, when the package isn't where plpython3u looks
DO $$
import drex_postgres.plpython
$$ LANGUAGE plpython3u;

-- What exists before this install, so the end of the script can tell what it created
DO $$
BEGIN
    PERFORM set_config('drex.install_existing', coalesce((SELECT string_agg(p.oid::text, ',') FROM pg_proc p
                                                          WHERE p.pronamespace = current_schema()::regnamespace), ''), true);
END
$$;

-- Every call is a billed request: VOLATILE and PARALLEL UNSAFE (the defaults), never STRICT, so a NULL input returns
-- NULL by the function's own rule and a NULL model means the default.

CREATE OR REPLACE FUNCTION drex_noul(input text, instructions text, criteria jsonb DEFAULT NULL,
                                     model text DEFAULT 'drex-v2.0')
RETURNS double precision LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $$
from drex_postgres.plpython import call
return call(plpy, "drex_noul", input, instructions, criteria, model)
$$;

CREATE OR REPLACE FUNCTION drex_noul(input jsonb, instructions text, criteria jsonb DEFAULT NULL,
                                     model text DEFAULT 'drex-v2.0')
RETURNS double precision LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $$
from drex_postgres.plpython import call
return call(plpy, "drex_noul", input, instructions, criteria, model, json_input=True)
$$;

CREATE OR REPLACE FUNCTION drex_choice(input text, instructions text, labels jsonb, model text DEFAULT 'drex-v2.0')
RETURNS jsonb LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $$
from drex_postgres.plpython import call
return call(plpy, "drex_choice", input, instructions, labels, model)
$$;

CREATE OR REPLACE FUNCTION drex_choice(input jsonb, instructions text, labels jsonb, model text DEFAULT 'drex-v2.0')
RETURNS jsonb LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $$
from drex_postgres.plpython import call
return call(plpy, "drex_choice", input, instructions, labels, model, json_input=True)
$$;

CREATE OR REPLACE FUNCTION drex_score(input text, instructions text, levels jsonb, model text DEFAULT 'drex-v2.0')
RETURNS jsonb LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $$
from drex_postgres.plpython import call
return call(plpy, "drex_score", input, instructions, levels, model)
$$;

CREATE OR REPLACE FUNCTION drex_score(input jsonb, instructions text, levels jsonb, model text DEFAULT 'drex-v2.0')
RETURNS jsonb LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $$
from drex_postgres.plpython import call
return call(plpy, "drex_score", input, instructions, levels, model, json_input=True)
$$;

CREATE OR REPLACE FUNCTION drex_decide(state jsonb, questions jsonb, model text DEFAULT 'drex-v2.0')
RETURNS jsonb LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $$
from drex_postgres.plpython import call
return call(plpy, "drex_decide", state, questions, model)
$$;

CREATE OR REPLACE FUNCTION drex_decide_image(state jsonb, images bytea[], questions jsonb,
                                             model text DEFAULT 'drex-max')
RETURNS jsonb LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $$
from drex_postgres.plpython import call
return call(plpy, "drex_decide_image", state, images, questions, model)
$$;

CREATE OR REPLACE FUNCTION drex_decide_batch(states jsonb[], questions jsonb, model text DEFAULT 'drex-v2.0',
                                             concurrency integer DEFAULT 8)
RETURNS jsonb[] LANGUAGE plpython3u VOLATILE PARALLEL UNSAFE AS $$
from drex_postgres.plpython import call
return call(plpy, "drex_decide_batch", states, questions, model, concurrency)
$$;

REVOKE ALL ON FUNCTION
    drex_noul(text, text, jsonb, text), drex_noul(jsonb, text, jsonb, text),
    drex_choice(text, text, jsonb, text), drex_choice(jsonb, text, jsonb, text),
    drex_score(text, text, jsonb, text), drex_score(jsonb, text, jsonb, text),
    drex_decide(jsonb, jsonb, text), drex_decide_image(jsonb, bytea[], jsonb, text),
    drex_decide_batch(jsonb[], jsonb, text, integer)
FROM PUBLIC;

-- Default privileges may grant new functions to other roles too, which could then spend the key. What this install
-- created keeps its owner's privileges alone; grants made since an earlier install are kept
DO $grants$
DECLARE
    existing oid[] := string_to_array(nullif(current_setting('drex.install_existing'), ''), ',')::oid[];
    granted record;
    revoked text[] := '{}';
BEGIN
    FOR granted IN
        SELECT DISTINCT p.oid::regprocedure::text AS object, a.grantee::regrole::text AS grantee
        FROM pg_proc p, aclexplode(p.proacl) a
        WHERE p.pronamespace = current_schema()::regnamespace AND p.proname LIKE 'drex\_%'
          AND p.oid <> ALL (coalesce(existing, '{}')) AND a.grantee NOT IN (0, p.proowner)
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %s', granted.object, granted.grantee);
        revoked := revoked || granted.grantee;
    END LOOP;
    IF cardinality(revoked) > 0 THEN
        RAISE NOTICE 'drex: revoked the default grants to % on what this install created; grant the functions to the roles that may spend the key',
            (SELECT string_agg(DISTINCT r, ', ') FROM unnest(revoked) r);
    END IF;
END
$grants$;

COMMIT;
