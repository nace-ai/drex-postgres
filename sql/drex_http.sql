-- Drex for PostgreSQL: the pgsql-http variant, for managed Postgres that offers the http extension but not plpython3u
-- (Supabase, among those checked; see README.md). The same functions and results as sql/drex.sql, in PL/pgSQL, one
-- request per row at a time.
--
-- Needs the http extension (pgsql-http, 1.6 or later). Run as the role that will own the functions and the key,
-- with the schema the functions go in first on search_path:
--
--   CREATE EXTENSION IF NOT EXISTS http;            -- WITH SCHEMA extensions, on Supabase
--   SET search_path = public;
--   \i sql/drex_http.sql
--   INSERT INTO drex_settings (api_key) VALUES (:'key');   -- the key from \getenv key NACE_API_KEY, or the editor
--
-- The key lives in drex_settings, which only its owner can read. The public functions are SECURITY DEFINER with a
-- pinned search_path, so they read it for their callers; they are revoked from PUBLIC: GRANT EXECUTE to the roles
-- that may spend the key. The helpers (_drex_*) are for the owner alone.

BEGIN;

-- Pins every function's search_path (SET search_path FROM CURRENT) to this schema, the http extension's, and pg_temp
-- last, so no other schema can stand in for a table or function they use
DO $install$
DECLARE
    http_schema name;
BEGIN
    SELECT n.nspname INTO http_schema
    FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname = 'http';
    IF http_schema IS NULL THEN
        RAISE EXCEPTION 'drex: install the http extension first: CREATE EXTENSION http';
    END IF;
    IF current_schema() IS NULL THEN
        RAISE EXCEPTION 'drex: search_path names no schema to install into';
    END IF;
    -- Loads pgsql-http, which defines http.timeout_msec: until then only a superuser may name it in a function's SET
    -- clause, and the install would fail for any other owner (Supabase's postgres)
    EXECUTE format('SELECT count(*) FROM %I.http_list_curlopt()', http_schema);
    -- What exists before this install, so the end of the script can tell what it created
    PERFORM set_config('drex.install_existing', coalesce((SELECT string_agg(p.oid::text, ',') FROM pg_proc p
                                                          WHERE p.pronamespace = quote_ident(current_schema())::regnamespace), ''), true);
    PERFORM set_config('drex.install_new_table', (to_regclass(format('%I.drex_settings', current_schema())) IS NULL)::text, true);
    PERFORM set_config('search_path', format('%I, %I, pg_temp', current_schema(), http_schema), true);
END
$install$;

CREATE TABLE IF NOT EXISTS drex_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    api_key text NOT NULL,
    base_url text NOT NULL DEFAULT 'https://console.nace.ai'
);
-- Optional, and used only when base_url is OpenRouter: its key (instead of api_key), and the app identity it
-- attributes requests to, in place of https://nace.ai and Drex for PostgreSQL
ALTER TABLE drex_settings ADD COLUMN IF NOT EXISTS openrouter_api_key text;
ALTER TABLE drex_settings ADD COLUMN IF NOT EXISTS openrouter_referer text;
ALTER TABLE drex_settings ADD COLUMN IF NOT EXISTS openrouter_title text;
REVOKE ALL ON drex_settings FROM PUBLIC;

-- drex-postgres-http/<version>, sent as User-Agent and X-Nace-Client
CREATE OR REPLACE FUNCTION _drex_client_name() RETURNS text
LANGUAGE sql IMMUTABLE SET search_path FROM CURRENT AS $$ SELECT 'drex-postgres-http/0.3.0' $$;

--------------------------------------------------------------------------------------------------------------------
-- Questions: the checks every function makes before it sends anything (drex_postgres/questions.py)
--------------------------------------------------------------------------------------------------------------------

-- One label as {"label"} or {"label", "description"}; a JSON null description is no description
CREATE OR REPLACE FUNCTION _drex_label(fn text, name text, shape text, label jsonb, description jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path FROM CURRENT AS $$
BEGIN
    IF label IS NULL OR jsonb_typeof(label) = 'null' THEN
        RAISE EXCEPTION '%: % labels must not be NULL', fn, name USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF jsonb_typeof(label) <> 'string'
       OR (description IS NOT NULL AND jsonb_typeof(description) NOT IN ('string', 'null')) THEN
        RAISE EXCEPTION '%', shape USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF description IS NULL OR jsonb_typeof(description) = 'null' THEN
        RETURN jsonb_build_object('label', label);
    END IF;
    RETURN jsonb_build_object('label', label, 'description', description);
END
$$;

-- The labels of criteria: a list of labels, a list of {"label", "description"}, or (allow_object) an object of label
-- to description
CREATE OR REPLACE FUNCTION _drex_labels(fn text, name text, value jsonb, allow_object boolean) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path FROM CURRENT AS $$
DECLARE
    shape text := format('%s: %s must be a JSON array of labels or of {"label", "description"} objects', fn, name)
        || CASE WHEN allow_object THEN ', or an object of label to description' ELSE '' END;
    result jsonb := '[]';
    element jsonb;
    key text;
BEGIN
    IF jsonb_typeof(value) = 'array' THEN
        FOR element IN SELECT e FROM jsonb_array_elements(value) WITH ORDINALITY AS a(e, i) ORDER BY i LOOP
            IF jsonb_typeof(element) = 'object' THEN
                IF EXISTS (SELECT FROM jsonb_object_keys(element) k WHERE k NOT IN ('label', 'description')) THEN
                    RAISE EXCEPTION '%', shape USING ERRCODE = 'invalid_parameter_value';
                END IF;
                result := result || jsonb_build_array(
                    _drex_label(fn, name, shape, element -> 'label', element -> 'description'));
            ELSE
                result := result || jsonb_build_array(_drex_label(fn, name, shape, element, NULL));
            END IF;
        END LOOP;
    ELSIF jsonb_typeof(value) = 'object' AND allow_object THEN
        FOR key, element IN SELECT * FROM jsonb_each(value) LOOP
            result := result || jsonb_build_array(_drex_label(fn, name, shape, to_jsonb(key), element));
        END LOOP;
    ELSE
        RAISE EXCEPTION '%', shape USING ERRCODE = 'invalid_parameter_value';
    END IF;
    RETURN result;
END
$$;

CREATE OR REPLACE FUNCTION _drex_question_type(fn text, name text, question_type text) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path FROM CURRENT AS $$
BEGIN
    IF question_type IN ('noul', 'choice', 'score') THEN
        RETURN question_type;
    END IF;
    IF question_type IN ('multiple_choice', 'box') THEN
        RAISE EXCEPTION '%: %: % questions are not supported, since Drex has no % question',
            fn, name, question_type, question_type USING ERRCODE = 'invalid_parameter_value';
    END IF;
    RAISE EXCEPTION '%: %.type must be noul, choice or score, not ''%''', fn, name, question_type
        USING ERRCODE = 'invalid_parameter_value';
END
$$;

-- Checks a question's labels and text, and returns it as Drex reads it, with instructions always a string
CREATE OR REPLACE FUNCTION _drex_question(fn text, name text, question_type text, instructions text, labels jsonb,
                                          from_object boolean) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path FROM CURRENT AS $$
DECLARE
    found text[] := ARRAY(SELECT l ->> 'label' FROM jsonb_array_elements(labels) WITH ORDINALITY AS a(l, i) ORDER BY i);
    kind text := CASE question_type WHEN 'choice' THEN 'labels' ELSE 'levels' END;
    duplicate text;
    question jsonb := jsonb_build_object('type', question_type, 'instructions', coalesce(instructions, ''));
BEGIN
    IF question_type = 'noul' THEN
        -- No labels is a noul without criteria
        IF NOT (found <@ ARRAY['true', 'false'])
           OR cardinality(found) <> (SELECT count(DISTINCT f) FROM unnest(found) f)
           OR NOT (cardinality(found) = 0 OR from_object OR found @> ARRAY['true', 'false']) THEN
            RAISE EXCEPTION '%: % labels must be exactly true and false, got [%]', fn, name, array_to_string(found, ', ')
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF coalesce(instructions, '') = '' AND NOT EXISTS (
            SELECT FROM jsonb_array_elements(labels) l WHERE coalesce(l ->> 'description', '') <> '') THEN
            RAISE EXCEPTION '%: % needs instructions, or a description of true or false', fn, name
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        -- Labels alone are the same as no criteria
        IF EXISTS (SELECT FROM jsonb_array_elements(labels) l WHERE l ? 'description') THEN
            question := question || jsonb_build_object('criteria',
                (SELECT jsonb_object_agg(l ->> 'label', l -> 'description') FROM jsonb_array_elements(labels) l));
        END IF;
        RETURN question;
    END IF;
    IF cardinality(found) NOT BETWEEN 2 AND 255 THEN
        RAISE EXCEPTION '%: % takes 2 to 255 %, got %', fn, name, kind, cardinality(found)
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF '' = ANY (found) THEN
        RAISE EXCEPTION '%: % % must not be empty', fn, name, kind USING ERRCODE = 'invalid_parameter_value';
    END IF;
    SELECT f INTO duplicate FROM unnest(found) WITH ORDINALITY AS a(f, i) GROUP BY f HAVING count(*) > 1
    ORDER BY min(i) LIMIT 1;
    IF duplicate IS NOT NULL THEN
        RAISE EXCEPTION '%: % % must be unique; ''%'' is a duplicate', fn, name, kind, duplicate
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF question_type = 'score' THEN
        IF EXISTS (SELECT FROM jsonb_array_elements(labels) l WHERE l ? 'description') THEN
            RAISE EXCEPTION '%: score levels take no description; describe them in instructions', fn
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        RETURN question || jsonb_build_object('criteria', to_jsonb(found));
    END IF;
    -- A choice's options as [label, description] pairs, since a jsonb object would lose their order; the request sends
    -- them as an object in this order (_drex_questions_json)
    RETURN question || jsonb_build_object('criteria',
        (SELECT jsonb_agg(jsonb_build_array(l -> 'label', coalesce(l -> 'description', 'null')) ORDER BY i)
         FROM jsonb_array_elements(labels) WITH ORDINALITY AS a(l, i)));
END
$$;

-- The one question of drex_noul, drex_choice or drex_score, as Drex reads it
CREATE OR REPLACE FUNCTION _drex_typed_question(fn text, question_type text, instructions text, criteria jsonb)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path FROM CURRENT AS $$
DECLARE
    argument text := CASE question_type WHEN 'noul' THEN 'criteria' WHEN 'choice' THEN 'labels' ELSE 'levels' END;
BEGIN
    IF criteria IS NULL AND question_type <> 'noul' THEN
        RAISE EXCEPTION '%: % must not be NULL', fn, argument USING ERRCODE = 'invalid_parameter_value';
    END IF;
    RETURN _drex_question(fn, question_type, question_type, instructions,
        CASE WHEN criteria IS NULL THEN '[]' ELSE _drex_labels(fn, argument, criteria, question_type <> 'score') END,
        coalesce(jsonb_typeof(criteria) = 'object', false));
END
$$;

-- The questions of drex_decide and its relatives, checked, as Drex reads them
CREATE OR REPLACE FUNCTION _drex_questions(fn text, questions jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path FROM CURRENT AS $$
DECLARE
    id text;
    entry jsonb;
    name text;
    question_type text;
    parsed jsonb := '[]';
    result jsonb := '{}';
    item jsonb;
    seen text[] := '{}';
BEGIN
    IF jsonb_typeof(questions) <> 'object' THEN
        RAISE EXCEPTION '%: questions must be a JSON object of question id to question', fn
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    FOR id, entry IN SELECT * FROM jsonb_each(questions) LOOP
        name := 'questions.' || id;
        IF jsonb_typeof(entry) <> 'object' THEN
            RAISE EXCEPTION '%: % must be an object with type, instructions and criteria', fn, name
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        SELECT k INTO question_type FROM jsonb_object_keys(entry) k
        WHERE k NOT IN ('type', 'instructions', 'criteria') LIMIT 1;
        IF question_type IS NOT NULL THEN
            RAISE EXCEPTION '%: % has an unknown key ''%''; use type, instructions and criteria', fn, name, question_type
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF jsonb_typeof(entry -> 'type') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION '%: %.type must be noul, choice or score', fn, name USING ERRCODE = 'invalid_parameter_value';
        END IF;
        question_type := _drex_question_type(fn, name, entry ->> 'type');
        IF jsonb_typeof(entry -> 'instructions') NOT IN ('string', 'null') THEN
            RAISE EXCEPTION '%: %.instructions must be a string', fn, name USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF coalesce(jsonb_typeof(entry -> 'criteria'), 'null') = 'null' AND question_type <> 'noul' THEN
            RAISE EXCEPTION '%: % needs criteria', fn, name USING ERRCODE = 'invalid_parameter_value';
        END IF;
        parsed := parsed || jsonb_build_array(jsonb_build_object(
            'id', id, 'type', question_type, 'instructions', entry ->> 'instructions',
            'labels', CASE WHEN coalesce(jsonb_typeof(entry -> 'criteria'), 'null') = 'null' THEN '[]'
                      ELSE _drex_labels(fn, name || '.criteria', entry -> 'criteria', question_type <> 'score') END,
            'from_object', coalesce(jsonb_typeof(entry -> 'criteria') = 'object', false)));
    END LOOP;
    IF jsonb_array_length(parsed) NOT BETWEEN 1 AND 512 THEN
        RAISE EXCEPTION '%: questions takes 1 to 512 questions, got %', fn, jsonb_array_length(parsed)
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    FOR item IN SELECT jsonb_array_elements(parsed) LOOP
        id := item ->> 'id';
        IF btrim(id) = '' THEN
            RAISE EXCEPTION '%: question ids must not be empty', fn USING ERRCODE = 'invalid_parameter_value';
        END IF;
        -- Drex collects answers in a plain object, whose __proto__ is not a key, so it would bill the request and
        -- drop the answer
        IF id = '__proto__' THEN
            RAISE EXCEPTION '%: the question id ''__proto__'' is not allowed: Drex can''t answer it', fn
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF lower(id) = ANY (seen) THEN
            RAISE EXCEPTION '%: question ids must be unique ignoring case; ''%'' is a duplicate', fn, id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        seen := seen || lower(id);
        result := result || jsonb_build_object(id, _drex_question(fn, 'questions.' || id, item ->> 'type',
            item ->> 'instructions', item -> 'labels', (item ->> 'from_object')::boolean));
    END LOOP;
    RETURN result;
END
$$;

-- The questions as the request's JSON: a choice's options as an object in the order they were written, which only
-- json, not jsonb, keeps
CREATE OR REPLACE FUNCTION _drex_questions_json(questions jsonb) RETURNS json
LANGUAGE sql IMMUTABLE SET search_path FROM CURRENT AS $$
    SELECT json_object_agg(id, CASE WHEN question ->> 'type' = 'choice' THEN json_build_object(
               'type', question -> 'type', 'instructions', question -> 'instructions', 'criteria',
               (SELECT json_object_agg(option ->> 0, option -> 1 ORDER BY i)
                FROM jsonb_array_elements(question -> 'criteria') WITH ORDINALITY AS o(option, i)))
           ELSE question::json END ORDER BY n)
    FROM jsonb_each(questions) WITH ORDINALITY AS q(id, question, n)
$$;

--------------------------------------------------------------------------------------------------------------------
-- Answers, in the shared SQL surface's shapes
--------------------------------------------------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION _drex_number(fn text, id text, object jsonb, key text) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path FROM CURRENT AS $$
BEGIN
    IF jsonb_typeof(object -> key) IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION '%: Drex''s answer to question ''%'' has no number %', fn, id, key
            USING ERRCODE = 'external_routine_exception';
    END IF;
    RETURN object -> key;
END
$$;

-- A noul's probability, or a choice's or score's object with its probabilities as rows, a choice's sorted by label
-- and a score's by index
CREATE OR REPLACE FUNCTION _drex_answer(fn text, id text, question jsonb, body jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path FROM CURRENT AS $$
DECLARE
    answer jsonb := body -> 'answers' -> id;
    rows jsonb;
BEGIN
    IF jsonb_typeof(answer) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION '%: Drex''s response has no answer to question ''%''', fn, id
            USING ERRCODE = 'external_routine_exception';
    END IF;
    IF question ->> 'type' = 'noul' THEN
        RETURN _drex_number(fn, id, answer, 'noul');
    END IF;
    IF question ->> 'type' = 'choice' THEN
        IF jsonb_typeof(answer -> 'choice') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION '%: Drex''s answer to question ''%'' has no choice', fn, id
                USING ERRCODE = 'external_routine_exception';
        END IF;
        SELECT jsonb_agg(jsonb_build_object('value', label,
                   'probability', _drex_number(fn, id, answer -> 'probabilities', label)) ORDER BY label COLLATE "C")
        INTO rows FROM jsonb_array_elements(question -> 'criteria') option, LATERAL (SELECT option ->> 0 AS label) l;
        RETURN jsonb_build_object('choice', answer -> 'choice', 'probabilities', rows,
                                  'confidence', _drex_number(fn, id, answer, 'confidence'));
    END IF;
    SELECT jsonb_agg(jsonb_build_object('index', i - 1, 'value', level,
               'probability', _drex_number(fn, id, answer -> 'probabilities', (i - 1)::text)) ORDER BY i)
    INTO rows FROM jsonb_array_elements_text(question -> 'criteria') WITH ORDINALITY AS a(level, i);
    RETURN jsonb_build_object('score', _drex_number(fn, id, answer, 'score'), 'probabilities', rows,
                              'confidence', _drex_number(fn, id, answer, 'confidence'));
END
$$;

--------------------------------------------------------------------------------------------------------------------
-- Calls (drex_postgres/client.py): retries, and which failures fail the query
--------------------------------------------------------------------------------------------------------------------

-- The key and base URL. A custom base URL receives the key and the rows, so it must be HTTPS, except on this machine.
-- On OpenRouter the key is openrouter_api_key when set, else api_key, and a nace_ key is refused
DROP FUNCTION IF EXISTS _drex_settings();
CREATE FUNCTION _drex_settings(OUT api_key text, OUT base_url text, OUT open_router boolean,
                               OUT referer text, OUT title text)
LANGUAGE plpgsql STABLE SET search_path FROM CURRENT AS $$
DECLARE
    host text;
    openrouter_key text;
BEGIN
    SELECT s.api_key, rtrim(s.base_url, '/'), nullif(btrim(s.openrouter_api_key), ''),
           coalesce(nullif(btrim(s.openrouter_referer), ''), 'https://nace.ai'),
           coalesce(nullif(btrim(s.openrouter_title), ''), 'Drex for PostgreSQL')
    INTO api_key, base_url, openrouter_key, referer, title FROM drex_settings s;
    host := lower(substring(base_url FROM '^[a-zA-Z][a-zA-Z0-9+.-]*://(?:[^/@]*@)?(\[[^]]*\]|[^:/?#]*)'));
    IF NOT (base_url ILIKE 'https://_%' OR (base_url ILIKE 'http://%' AND host IN ('localhost', '127.0.0.1', '[::1]'))) THEN
        RAISE EXCEPTION 'drex: drex_settings.base_url must be an https:// URL (http:// only for localhost, 127.0.0.1 or [::1])'
            USING ERRCODE = 'invalid_authorization_specification';
    END IF;
    open_router := host = 'openrouter.ai' OR host LIKE '%.openrouter.ai';
    IF open_router THEN
        api_key := coalesce(openrouter_key, api_key);
        IF btrim(api_key) LIKE 'nace\_%' THEN
            RAISE EXCEPTION 'drex: a Nace API key (nace_...) is never sent to OpenRouter; set drex_settings.openrouter_api_key (OPENROUTER_API_KEY in the plpython variant) to an OpenRouter key'
                USING ERRCODE = 'invalid_authorization_specification';
        END IF;
    END IF;
    api_key := btrim(api_key);
    IF coalesce(api_key, '') = '' THEN
        RAISE EXCEPTION 'drex: no API key; its owner sets it with INSERT INTO drex_settings (api_key) VALUES (...)'
            USING ERRCODE = 'invalid_authorization_specification';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION _drex_try_jsonb(content text) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path FROM CURRENT AS $$
BEGIN
    RETURN content::jsonb;
EXCEPTION WHEN invalid_text_representation THEN
    RETURN NULL;
END
$$;

-- `path: message`, or the message alone for an issue with no path; a row's issues cut a path under `state`, which
-- names the row's own keys, to `state`
CREATE OR REPLACE FUNCTION _drex_issues(body jsonb, of_row boolean) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path FROM CURRENT AS $$
    SELECT string_agg(
        CASE WHEN p = '' THEN m ELSE (CASE WHEN of_row AND (p = 'state' OR p LIKE 'state.%') THEN 'state' ELSE p END) || ': ' || m END,
        '; ' ORDER BY i)
    FROM jsonb_array_elements(CASE WHEN jsonb_typeof(body -> 'error' -> 'issues') = 'array'
                                   THEN body -> 'error' -> 'issues' ELSE '[]' END) WITH ORDINALITY AS a(issue, i),
         LATERAL (SELECT coalesce(issue ->> 'path', '') AS p, coalesce(issue ->> 'message', '') AS m) x
$$;

-- One request, retried: at most 5 attempts within 120 s of the first, waiting for retry-after-ms (or retry-after) up
-- to 30 s, else 1, 2, 4 and 8 s less up to 25 % jitter. Returns the 2xx body, or why the row has none; raises for
-- 401, 402, a 422 not about the row alone, and a TLS failure. The 60 s timeout is this function's own setting
CREATE OR REPLACE FUNCTION _drex_call(fn text, method text, path text, body text, idempotency_key text,
                                      OUT response jsonb, OUT failure text)
LANGUAGE plpgsql VOLATILE SET search_path FROM CURRENT SET http.timeout_msec = 60000 AS $$
DECLARE
    settings record := _drex_settings();
    headers http_header[];
    started timestamptz := clock_timestamp();
    attempt integer := 0;
    status integer;
    content text;
    response_headers http_header[];
    transport text;
    error_body jsonb;
    message text;
    request_id text;
    wait_ms double precision;
    row_refusal boolean;
    refused text;
BEGIN
    -- Any role may call http_set_curlopt, and the options it sets (shared with pgsql-http 1.7's http.curlopt_*
    -- settings) apply to this request, which carries the key. A caller who can't read the key may set timeouts,
    -- keepalive and, over HTTPS, a proxy, never what TLS trusts: else it could turn off certificate checks and read the
    -- key through its own proxy
    IF NOT has_table_privilege(session_user, 'drex_settings', 'SELECT') THEN
        SELECT string_agg(o.curlopt, ', ' ORDER BY o.curlopt) INTO refused FROM http_list_curlopt() o
        WHERE o.curlopt NOT IN ('CURLOPT_TIMEOUT', 'CURLOPT_TIMEOUT_MS', 'CURLOPT_CONNECTTIMEOUT', 'CURLOPT_CONNECTTIMEOUT_MS',
                                'CURLOPT_TCP_KEEPALIVE', 'CURLOPT_TCP_KEEPIDLE', 'CURLOPT_IPRESOLVE', 'CURLOPT_USERAGENT')
          AND NOT (settings.base_url ILIKE 'https://%' AND o.curlopt IN (
              'CURLOPT_PROXY', 'CURLOPT_PROXYPORT', 'CURLOPT_PROXYUSERNAME', 'CURLOPT_PROXYPASSWORD', 'CURLOPT_PRE_PROXY',
              'CURLOPT_PROXY_CAINFO', 'CURLOPT_PROXY_TLSAUTH_USERNAME', 'CURLOPT_PROXY_TLSAUTH_PASSWORD',
              'CURLOPT_PROXY_TLSAUTH_TYPE', 'CURLOPT_DNS_SERVERS'));
        IF refused IS NOT NULL THEN
            RAISE EXCEPTION '%: this session set http options that would apply to the request carrying the API key (%); call http_reset_curlopt() first, or ask the functions'' owner',
                fn, refused USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    headers := ARRAY[
        ('Authorization', 'Bearer ' || settings.api_key)::http_header,
        ('Accept', 'application/json')::http_header,
        ('User-Agent', _drex_client_name())::http_header,
        ('X-Nace-Client', _drex_client_name())::http_header];
    IF idempotency_key IS NOT NULL THEN
        headers := headers || ('Idempotency-Key', idempotency_key)::http_header;
    END IF;
    IF settings.open_router THEN
        headers := headers || ('HTTP-Referer', settings.referer)::http_header
                           || ('X-OpenRouter-Title', settings.title)::http_header;
    END IF;
    LOOP
        attempt := attempt + 1;
        status := 0;
        transport := NULL;
        BEGIN
            SELECT r.status, r.content, r.headers INTO status, content, response_headers
            FROM http((method, settings.base_url || path, headers,
                       CASE WHEN body IS NULL THEN NULL ELSE 'application/json' END, body)::http_request) r;
        EXCEPTION WHEN SQLSTATE 'XX000' THEN
            -- pgsql-http reports a cancel or a statement_timeout during the request this way too
            IF SQLERRM LIKE 'canceling statement%' THEN
                RAISE EXCEPTION '%', SQLERRM USING ERRCODE = 'query_canceled';
            END IF;
            transport := SQLERRM;
        END;
        IF status BETWEEN 200 AND 299 THEN
            response := _drex_try_jsonb(content);
            IF response IS NULL THEN
                RAISE EXCEPTION '%: Drex answered with a body that is not JSON', fn
                    USING ERRCODE = 'external_routine_exception';
            END IF;
            RETURN;
        END IF;
        IF status = 0 AND (transport ILIKE '%ssl%' OR transport ILIKE '%certificate%') THEN
            RAISE EXCEPTION '%: could not reach Drex over TLS: %', fn, transport USING ERRCODE = 'external_routine_exception';
        END IF;
        error_body := _drex_try_jsonb(content);
        message := coalesce(CASE WHEN jsonb_typeof(error_body -> 'error') = 'string' THEN error_body ->> 'error' END,
                            error_body -> 'error' ->> 'message', '');
        request_id := coalesce(error_body ->> 'request_id',
                               (SELECT h.value FROM unnest(response_headers) h WHERE lower(h.field) = 'x-request-id' LIMIT 1));
        request_id := CASE WHEN request_id IS NULL THEN '' ELSE ' (request ' || request_id || ')' END;
        -- A 422 is about the row, not the query's constants, when the request is over the token limit or its issues
        -- are all under state or images
        row_refusal := status = 422 AND (message LIKE 'Request too long:%' OR (
            jsonb_typeof(error_body -> 'error' -> 'issues') = 'array'
            AND jsonb_array_length(error_body -> 'error' -> 'issues') > 0
            AND NOT EXISTS (
                SELECT FROM jsonb_array_elements(error_body -> 'error' -> 'issues') issue
                WHERE NOT coalesce(issue ->> 'path' ~ '^(state|images)(\.|$)', false))));
        IF status = 401 THEN
            RAISE EXCEPTION '%: %', fn, coalesce(nullif(message, ''), 'the API key was refused (HTTP 401)') || request_id
                USING ERRCODE = 'invalid_authorization_specification';
        ELSIF status = 402 THEN
            RAISE EXCEPTION '%: % Billing: %', fn, coalesce(nullif(message, ''), 'the account has no funds left (HTTP 402).'),
                CASE WHEN settings.open_router THEN 'https://openrouter.ai/settings/credits'
                     ELSE settings.base_url || '/dashboard/billing' END || request_id
                USING ERRCODE = 'insufficient_resources';
        ELSIF status = 422 AND NOT row_refusal THEN
            RAISE EXCEPTION '%: Drex refused the request: %', fn,
                coalesce(_drex_issues(error_body, false), nullif(message, ''), 'HTTP 422') || request_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF (status = 0 OR status IN (408, 429, 500, 502, 503, 504, 529)) AND attempt < 5 THEN
            wait_ms := NULL;
            BEGIN
                wait_ms := (SELECT h.value FROM unnest(response_headers) h WHERE lower(h.field) = 'retry-after-ms' LIMIT 1)::double precision;
            EXCEPTION WHEN invalid_text_representation THEN NULL;
            END;
            IF wait_ms IS NULL OR wait_ms < 0 THEN
                BEGIN
                    wait_ms := 1000 * (SELECT h.value FROM unnest(response_headers) h WHERE lower(h.field) = 'retry-after' LIMIT 1)::double precision;
                EXCEPTION WHEN invalid_text_representation THEN wait_ms := NULL;
                END;
            END IF;
            IF wait_ms IS NULL OR wait_ms < 0 THEN
                wait_ms := (1000 << least(attempt - 1, 4)) * (1 - random() / 4);
            END IF;
            wait_ms := least(wait_ms, 30000);
            IF clock_timestamp() + wait_ms * interval '1 millisecond' <= started + interval '120 seconds' THEN
                PERFORM pg_sleep(wait_ms / 1000);
                CONTINUE;
            END IF;
        END IF;
        -- The response's own message, else its status; a 422 that reaches here refused only the row, and its message
        -- may quote the row, so only its issues are kept
        failure := CASE WHEN status = 0 THEN coalesce(transport, 'no response')
                        ELSE 'HTTP ' || status || coalesce(': ' || nullif(
                            CASE WHEN status = 422 THEN _drex_issues(error_body, true) ELSE message END, ''), '')
                             || request_id END;
        IF status = 0 OR status IN (408, 429, 500, 502, 503, 504, 529) THEN
            failure := format('no answer after %s attempts, the last %s', attempt, failure);
        END IF;
        RETURN;
    END LOOP;
END
$$;

-- The model a request sends, from GET /v1/models, cached for the session per base URL (in the drex.models setting)
-- and fetched again for a model the cache lacks. Only this check refuses an unregistered drex-* name
CREATE OR REPLACE FUNCTION _drex_model(fn text, model text, images boolean) RETURNS text
LANGUAGE plpgsql VOLATILE SET search_path FROM CURRENT AS $$
DECLARE
    settings record := _drex_settings();
    name text := CASE WHEN settings.open_router AND strpos(model, '/') = 0 THEN 'nace-ai/' || model ELSE model END;
    cache jsonb := _drex_try_jsonb(nullif(current_setting('drex.models', true), ''));
    models jsonb;
    listed jsonb;
    result record;
BEGIN
    IF cache ->> 'base_url' IS DISTINCT FROM settings.base_url OR NOT (cache -> 'models') @> jsonb_build_array(jsonb_build_object('name', name)) THEN
        result := _drex_call(fn, 'GET', CASE WHEN settings.open_router THEN '/v1/models?output_modalities=decisions'
                                             ELSE '/v1/models' END, NULL, NULL);
        IF result.failure IS NOT NULL THEN
            RAISE EXCEPTION '%: could not list Drex''s models: %', fn, result.failure USING ERRCODE = 'external_routine_exception';
        END IF;
        IF jsonb_typeof(result.response -> 'models') = 'array'
           AND NOT EXISTS (SELECT FROM jsonb_array_elements(result.response -> 'models') m WHERE jsonb_typeof(m -> 'name') IS DISTINCT FROM 'string') THEN
            -- A model reads images when it, or the version its alias_for names, is an image model
            SELECT coalesce(jsonb_agg(jsonb_build_object('name', m ->> 'name', 'images',
                       coalesce(CASE WHEN jsonb_typeof(m -> 'alias_for') = 'string' THEN m ->> 'alias_for' END, m ->> 'name')
                           IN ('drex-max', 'drex-max-v1.1')) ORDER BY i), '[]')
            INTO models FROM jsonb_array_elements(result.response -> 'models') WITH ORDINALITY AS a(m, i);
        ELSIF jsonb_typeof(result.response -> 'data') = 'array'
              AND NOT EXISTS (SELECT FROM jsonb_array_elements(result.response -> 'data') m WHERE jsonb_typeof(m -> 'id') IS DISTINCT FROM 'string') THEN
            -- OpenRouter's, of which only Nace's models are Drex
            SELECT coalesce(jsonb_agg(jsonb_build_object('name', m ->> 'id', 'images',
                       coalesce(m -> 'architecture' -> 'input_modalities' @> '["image"]', false)) ORDER BY i), '[]')
            INTO models FROM jsonb_array_elements(result.response -> 'data') WITH ORDINALITY AS a(m, i)
            WHERE m ->> 'id' LIKE 'nace-ai/%';
        ELSE
            RAISE EXCEPTION '%: GET /v1/models answered with an unexpected body', fn USING ERRCODE = 'external_routine_exception';
        END IF;
        cache := jsonb_build_object('base_url', settings.base_url, 'models', models);
        PERFORM set_config('drex.models', cache::text, false);
    END IF;
    SELECT m INTO listed FROM jsonb_array_elements(cache -> 'models') m WHERE m ->> 'name' = name;
    IF listed IS NULL THEN
        RAISE EXCEPTION '%: unknown model ''%''; GET /v1/models lists %', fn, model,
            (SELECT string_agg(m ->> 'name', ', ' ORDER BY i) FROM jsonb_array_elements(cache -> 'models') WITH ORDINALITY AS a(m, i))
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF images AND NOT (listed ->> 'images')::boolean THEN
        RAISE EXCEPTION '%: % does not read images; use an image model such as drex-max', fn, model
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    RETURN name;
END
$$;

CREATE OR REPLACE FUNCTION _drex_image_type(image bytea) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path FROM CURRENT AS $$
    SELECT CASE
        WHEN substring(image FROM 1 FOR 8) = '\x89504e470d0a1a0a'::bytea THEN 'image/png'
        WHEN substring(image FROM 1 FOR 3) = '\xffd8ff'::bytea THEN 'image/jpeg'
        WHEN substring(image FROM 1 FOR 4) = '\x52494646'::bytea AND substring(image FROM 9 FOR 4) = '\x57454250'::bytea
            THEN 'image/webp'
    END
$$;

-- One row's request: its response body, or NULL with a warning for a row with no answer (a body over 4 MiB, an image
-- that isn't a PNG, JPEG or WebP, a 422 about the row, retries running out)
CREATE OR REPLACE FUNCTION _drex_decide_row(fn text, row_name text, model text, state jsonb, questions jsonb,
                                            images bytea[]) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path FROM CURRENT AS $$
DECLARE
    settings record := _drex_settings();
    body_state jsonb := state;
    body_images jsonb;
    body_text text;
    bad integer;
    result record;
BEGIN
    SELECT i INTO bad FROM unnest(images) WITH ORDINALITY AS a(image, i) WHERE _drex_image_type(image) IS NULL ORDER BY i LIMIT 1;
    IF bad IS NOT NULL THEN
        RAISE WARNING '%: % returned NULL: image % is not a PNG, JPEG or WebP image', fn, row_name, bad;
        RETURN NULL;
    END IF;
    IF cardinality(images) > 0 AND settings.open_router THEN
        -- OpenRouter takes images as data URLs in state content parts after the state as text
        body_state := jsonb_build_array(jsonb_build_object('type', 'text', 'text',
                          CASE WHEN jsonb_typeof(state) = 'string' THEN state #>> '{}' ELSE state::text END))
            || (SELECT jsonb_agg(jsonb_build_object('type', 'image_url', 'image_url', jsonb_build_object('url',
                    'data:' || _drex_image_type(image) || ';base64,' || translate(encode(image, 'base64'), E'\n', ''))) ORDER BY i)
                FROM unnest(images) WITH ORDINALITY AS a(image, i));
    ELSIF cardinality(images) > 0 THEN
        body_images := (SELECT jsonb_agg(translate(encode(image, 'base64'), E'\n', '') ORDER BY i)
                        FROM unnest(images) WITH ORDINALITY AS a(image, i));
    END IF;
    body_text := CASE WHEN body_images IS NULL
        THEN json_build_object('state', body_state, 'model', model, 'questions', _drex_questions_json(questions))
        ELSE json_build_object('state', body_state, 'images', body_images, 'model', model,
                               'questions', _drex_questions_json(questions)) END::text;
    IF octet_length(body_text) > 4 * 1024 * 1024 THEN
        RAISE WARNING '%: % returned NULL: the request body is % bytes, over Drex''s limit of %', fn, row_name,
            octet_length(body_text),
            (4 * 1024 * 1024)::text || CASE WHEN cardinality(images) > 0 THEN '; resize the images first' ELSE '' END;
        RETURN NULL;
    END IF;
    -- The same for every attempt. Drex's /v1/systemone ignores it and bills only a 200, so a retry is billed twice
    -- only when a 200 was lost on its way back
    result := _drex_call(fn, 'POST', '/v1/systemone', body_text,
        encode(sha256(convert_to(model, 'UTF8') || '\x00'::bytea || convert_to(body_text, 'UTF8')), 'hex'));
    IF result.failure IS NOT NULL THEN
        RAISE WARNING '%: % returned NULL: %', fn, row_name, result.failure;
    END IF;
    RETURN result.response;
END
$$;

CREATE OR REPLACE FUNCTION _drex_typed(fn text, question_type text, input jsonb, instructions text, criteria jsonb,
                                       model text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SET search_path FROM CURRENT AS $$
DECLARE
    question jsonb := _drex_typed_question(fn, question_type, instructions, criteria);
    body jsonb;
BEGIN
    IF input IS NULL THEN
        RETURN NULL;
    END IF;
    body := _drex_decide_row(fn, 'a row', _drex_model(fn, coalesce(nullif(model, ''), 'drex-v2.0'), false), input,
                             jsonb_build_object(question_type, question), '{}');
    RETURN CASE WHEN body IS NULL THEN NULL ELSE _drex_answer(fn, question_type, question, body) END;
END
$$;

--------------------------------------------------------------------------------------------------------------------
-- The functions
--------------------------------------------------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION drex_noul(input text, instructions text, criteria jsonb DEFAULT NULL,
                                     model text DEFAULT 'drex-v2.0') RETURNS double precision
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER SET search_path FROM CURRENT AS $$
    SELECT (_drex_typed('drex_noul', 'noul', to_jsonb(input), instructions, criteria, model))::double precision
$$;

CREATE OR REPLACE FUNCTION drex_noul(input jsonb, instructions text, criteria jsonb DEFAULT NULL,
                                     model text DEFAULT 'drex-v2.0') RETURNS double precision
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER SET search_path FROM CURRENT AS $$
    SELECT (_drex_typed('drex_noul', 'noul', input, instructions, criteria, model))::double precision
$$;

CREATE OR REPLACE FUNCTION drex_choice(input text, instructions text, labels jsonb, model text DEFAULT 'drex-v2.0')
RETURNS jsonb
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER SET search_path FROM CURRENT AS $$
    SELECT _drex_typed('drex_choice', 'choice', to_jsonb(input), instructions, labels, model)
$$;

CREATE OR REPLACE FUNCTION drex_choice(input jsonb, instructions text, labels jsonb, model text DEFAULT 'drex-v2.0')
RETURNS jsonb
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER SET search_path FROM CURRENT AS $$
    SELECT _drex_typed('drex_choice', 'choice', input, instructions, labels, model)
$$;

CREATE OR REPLACE FUNCTION drex_score(input text, instructions text, levels jsonb, model text DEFAULT 'drex-v2.0')
RETURNS jsonb
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER SET search_path FROM CURRENT AS $$
    SELECT _drex_typed('drex_score', 'score', to_jsonb(input), instructions, levels, model)
$$;

CREATE OR REPLACE FUNCTION drex_score(input jsonb, instructions text, levels jsonb, model text DEFAULT 'drex-v2.0')
RETURNS jsonb
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER SET search_path FROM CURRENT AS $$
    SELECT _drex_typed('drex_score', 'score', input, instructions, levels, model)
$$;

CREATE OR REPLACE FUNCTION drex_decide(state jsonb, questions jsonb, model text DEFAULT 'drex-v2.0') RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER SET search_path FROM CURRENT AS $$
DECLARE
    drex_questions jsonb := CASE WHEN questions IS NULL THEN NULL ELSE _drex_questions('drex_decide', questions) END;
BEGIN
    IF state IS NULL OR drex_questions IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN _drex_decide_row('drex_decide', 'a row', _drex_model('drex_decide', coalesce(nullif(model, ''), 'drex-v2.0'), false),
                            state, drex_questions, '{}');
END
$$;

CREATE OR REPLACE FUNCTION drex_decide_image(state jsonb, images bytea[], questions jsonb, model text DEFAULT 'drex-max')
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER SET search_path FROM CURRENT AS $$
DECLARE
    drex_questions jsonb := CASE WHEN questions IS NULL THEN NULL ELSE _drex_questions('drex_decide_image', questions) END;
BEGIN
    IF array_ndims(images) > 1 THEN
        RAISE EXCEPTION 'drex_decide_image: images must be a one-dimensional array' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- A NULL image, or no images, sends nothing
    IF state IS NULL OR drex_questions IS NULL OR coalesce(cardinality(images), 0) = 0 OR array_position(images, NULL) IS NOT NULL THEN
        RETURN NULL;
    END IF;
    RETURN _drex_decide_row('drex_decide_image', 'a row',
                            _drex_model('drex_decide_image', coalesce(nullif(model, ''), 'drex-max'), true),
                            state, drex_questions, images);
END
$$;

-- One result per state, in order, with NULL for a NULL state or a failed row. This variant sends them one at a time:
-- `concurrency` is checked, for the same signature as the PL/Python functions, and otherwise unused
CREATE OR REPLACE FUNCTION drex_decide_batch(states jsonb[], questions jsonb, model text DEFAULT 'drex-v2.0',
                                             concurrency integer DEFAULT 8) RETURNS jsonb[]
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER SET search_path FROM CURRENT AS $$
DECLARE
    fn text := 'drex_decide_batch';
    drex_questions jsonb;
    sent_model text;
    results jsonb[] := '{}';
    position integer;
BEGIN
    IF coalesce(concurrency, 8) < 1 THEN
        RAISE EXCEPTION '%: concurrency must be at least 1', fn USING ERRCODE = 'invalid_parameter_value';
    END IF;
    IF array_ndims(states) > 1 THEN
        RAISE EXCEPTION '%: states must be a one-dimensional array', fn USING ERRCODE = 'invalid_parameter_value';
    END IF;
    drex_questions := CASE WHEN questions IS NULL THEN NULL ELSE _drex_questions(fn, questions) END;
    IF states IS NULL OR drex_questions IS NULL THEN
        RETURN NULL;
    END IF;
    FOR position IN 1 .. coalesce(cardinality(states), 0) LOOP
        IF states[position] IS NULL THEN
            results := results || NULL::jsonb;
            CONTINUE;
        END IF;
        sent_model := coalesce(sent_model, _drex_model(fn, coalesce(nullif(model, ''), 'drex-v2.0'), false));
        results := results || _drex_decide_row(fn, CASE WHEN cardinality(states) > 1 THEN 'row ' || position ELSE 'a row' END,
                                               sent_model, states[position], drex_questions, '{}');
    END LOOP;
    RETURN results;
END
$$;

REVOKE ALL ON FUNCTION
    drex_noul(text, text, jsonb, text), drex_noul(jsonb, text, jsonb, text),
    drex_choice(text, text, jsonb, text), drex_choice(jsonb, text, jsonb, text),
    drex_score(text, text, jsonb, text), drex_score(jsonb, text, jsonb, text),
    drex_decide(jsonb, jsonb, text), drex_decide_image(jsonb, bytea[], jsonb, text),
    drex_decide_batch(jsonb[], jsonb, text, integer),
    _drex_client_name(), _drex_label(text, text, text, jsonb, jsonb), _drex_labels(text, text, jsonb, boolean),
    _drex_question_type(text, text, text), _drex_question(text, text, text, text, jsonb, boolean),
    _drex_typed_question(text, text, text, jsonb), _drex_questions(text, jsonb), _drex_questions_json(jsonb),
    _drex_number(text, text, jsonb, text), _drex_answer(text, text, jsonb, jsonb), _drex_settings(),
    _drex_try_jsonb(text), _drex_issues(jsonb, boolean), _drex_call(text, text, text, text, text),
    _drex_model(text, text, boolean), _drex_image_type(bytea), _drex_decide_row(text, text, text, jsonb, jsonb, bytea[]),
    _drex_typed(text, text, jsonb, text, jsonb, text)
FROM PUBLIC;

-- Default privileges grant new objects to other roles too (Supabase's grant every table and function in public to
-- anon, authenticated and service_role), which could then read the key or spend it. What this install created keeps
-- its owner's privileges alone; grants made since an earlier install are kept
DO $grants$
DECLARE
    existing oid[] := string_to_array(nullif(current_setting('drex.install_existing'), ''), ',')::oid[];
    granted record;
    revoked text[] := '{}';
BEGIN
    FOR granted IN
        SELECT DISTINCT format('FUNCTION %s', p.oid::regprocedure) AS object, a.grantee::regrole::text AS grantee
        FROM pg_proc p, aclexplode(p.proacl) a
        WHERE p.pronamespace = quote_ident(current_schema())::regnamespace AND p.proname ~ '^_?drex_'
          AND p.oid <> ALL (coalesce(existing, '{}')) AND a.grantee NOT IN (0, p.proowner)
        UNION
        SELECT DISTINCT format('TABLE %s', c.oid::regclass), a.grantee::regrole::text
        FROM pg_class c, aclexplode(c.relacl) a
        WHERE c.oid = 'drex_settings'::regclass AND current_setting('drex.install_new_table')::boolean
          AND a.grantee NOT IN (0, c.relowner)
    LOOP
        EXECUTE format('REVOKE ALL ON %s FROM %s', granted.object, granted.grantee);
        revoked := revoked || granted.grantee;
    END LOOP;
    IF cardinality(revoked) > 0 THEN
        RAISE NOTICE 'drex: revoked the default grants to % on what this install created; grant the functions to the roles that may spend the key',
            (SELECT string_agg(DISTINCT r, ', ') FROM unnest(revoked) r);
    END IF;
END
$grants$;

COMMIT;
