"""Both variants in Postgres, against the mock inside the test container (docker/Dockerfile): sql/drex.sql in schema
drex_py and sql/drex_http.sql in schema drex_http. Every test runs on each.

    docker build -f docker/Dockerfile -t drex-postgres-test .
    docker run -d --rm --name drex-pg -p 55432:5432 -p 58787:8787 drex-postgres-test
    DREX_TEST_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres DREX_TEST_MOCK=http://127.0.0.1:58787 pytest
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from conftest import BLUE, MOCK_KEY, RED, Mock
from psycopg import errors

from drex_postgres import __version__

ROOT = Path(__file__).resolve().parent.parent
DSN = os.environ.get("DREX_TEST_DSN")
#: The mock as the tests reach it; Postgres reaches it at INSIDE
MOCK = os.environ.get("DREX_TEST_MOCK", "http://127.0.0.1:58787")
INSIDE = "http://127.0.0.1:8787"

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not DSN, reason="DREX_TEST_DSN names no test Postgres")]

TICKET = "I was charged twice for my March invoice. Please refund one of the charges."
NOUL = json.dumps({"a": {"type": "noul", "instructions": "Is the customer asking for a refund?"}})
COLOURS = json.dumps({"colour": {"type": "choice", "instructions": "Which colour is Image 1?", "criteria": ["red", "blue"]}})
CLIENTS = {"drex_py": f"drex-postgres/{__version__}", "drex_http": f"drex-postgres-http/{__version__}"}


@pytest.fixture(scope="module")
def installed() -> None:
    """Installs both variants into fresh schemas, and the test helpers."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS drex_py, drex_http, drex_test CASCADE")
        conn.execute("DROP ROLE IF EXISTS drex_analyst")
        conn.execute("CREATE SCHEMA drex_py; CREATE SCHEMA drex_http; CREATE SCHEMA drex_test")
        conn.execute("CREATE EXTENSION IF NOT EXISTS http")
        conn.execute("SET search_path = drex_py")
        conn.execute((ROOT / "sql" / "drex.sql").read_text())
        conn.execute("SET search_path = drex_http")
        conn.execute((ROOT / "sql" / "drex_http.sql").read_text())
        conn.execute("INSERT INTO drex_http.drex_settings (api_key) VALUES (%s)", [MOCK_KEY])
        # Test-only: the PL/Python functions read the backend's environment, which only this can change
        conn.execute(
            """
            CREATE FUNCTION drex_test.setenv(name text, value text) RETURNS void LANGUAGE plpython3u AS $$
            import os
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
            $$;
            CREATE FUNCTION drex_test.write_file(path text, content text) RETURNS void LANGUAGE plpython3u AS $$
            open(path, "w").write(content)
            $$;
            CREATE ROLE drex_analyst LOGIN PASSWORD 'analyst';
            GRANT USAGE ON SCHEMA drex_py, drex_http TO drex_analyst;
            """
        )


class Variant:
    def __init__(self, schema: str, conn: psycopg.Connection, mock: Mock, ns: str) -> None:
        self.schema = schema
        self.conn = conn
        self.mock = mock
        self.ns = ns
        self.warnings: list[str] = []
        conn.add_notice_handler(lambda diag: self.warnings.append(diag.message_primary or ""))

    def use(self, scenario: str = "ok", base_url: str | None = None) -> None:
        url = base_url or f"{INSIDE}/ns/{self.ns}/{scenario}"
        if self.schema == "drex_py":
            self.conn.execute("SELECT drex_test.setenv('NACE_BASE_URL', %s)", [url])
        else:
            self.conn.execute("UPDATE drex_http.drex_settings SET base_url = %s", [url])

    def one(self, sql: str, params: list | None = None):
        return self.conn.execute(sql, params).fetchone()[0]

    def requests(self) -> list[dict]:
        return self.mock.requests(self.ns)


@pytest.fixture(params=["drex_py", "drex_http"])
def db(request: pytest.FixtureRequest, installed: None) -> Iterator[Variant]:
    mock = Mock(MOCK)
    ns = re.sub(r"[^A-Za-z0-9_]", "_", request.node.name)[:60]
    mock.reset(ns)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"SET search_path = {request.param}")
        variant = Variant(request.param, conn, mock, ns)
        variant.use()
        yield variant


def test_every_function_answers_in_the_shared_shapes(db: Variant) -> None:
    categories = json.dumps({"billing": "Payments, invoices, refunds, or charges", "technical": "Bugs, errors, or outages",
                             "account": "Login, profile, or permissions", "other": None})  # fmt: skip
    noul, choice, score = db.conn.execute(
        """SELECT drex_noul(%(t)s, 'Is the customer asking for a refund?'),
                  drex_choice(%(t)s, 'What is this support ticket about?', %(c)s),
                  drex_score(%(t)s, 'How upset is the customer?', '["Calm", "Mildly annoyed", "Frustrated", "Angry"]',
                             model => 'drex-max')""",
        {"t": TICKET, "c": categories},
    ).fetchone()
    assert noul == 0.9868
    assert choice == {
        "choice": "billing",
        "probabilities": [
            {"value": "account", "probability": 0.0006},
            {"value": "billing", "probability": 0.9895},
            {"value": "other", "probability": 0.0049},
            {"value": "technical", "probability": 0.005},
        ],
        "confidence": 0.986,
    }
    assert score["score"] == 1.5306 and [row["index"] for row in score["probabilities"]] == [0, 1, 2, 3]
    requests = db.requests()
    assert [r["model"] for r in requests] == ["drex-v2.0", "drex-v2.0", "drex-max"]
    assert {r["x_nace_client"] for r in requests} == {CLIENTS[db.schema]}
    assert db.warnings == []


def test_text_and_jsonb_inputs_pick_their_overload(db: Variant) -> None:
    db.one("SELECT drex_noul('a literal', 'Refund?')")
    db.one("SELECT drex_noul(%s::text, 'Refund?')", [TICKET])
    db.one("SELECT drex_noul('{\"message\": \"refund me\"}'::jsonb, 'Refund?')")
    assert [r["state_type"] for r in db.requests()] == ["string", "string", "object"]


def test_drex_decide_returns_the_whole_body(db: Variant) -> None:
    questions = {"refund": {"type": "noul", "instructions": "Is the customer asking for a refund?"},
                 "team": {"type": "choice", "instructions": "Which team?", "criteria": ["billing", "tech"]},
                 "urgency": {"type": "score", "instructions": "How urgent?", "criteria": ["low", "high"]}}  # fmt: skip
    body = db.one("SELECT drex_decide(to_jsonb(%s::text), %s::jsonb)", [TICKET, json.dumps(questions)])
    assert set(body) == {"model", "answers", "usage", "evaluation_time_ms", "request_id"}
    assert body["answers"]["refund"]["noul"] == 0.9868
    [request] = db.requests()
    assert request["question_count"] == 3


def test_drex_decide_image_reads_images_in_order(db: Variant) -> None:
    colours = [
        db.one("SELECT drex_decide_image('\"\"', ARRAY[%s::bytea], %s) -> 'answers' -> 'colour' ->> 'choice'", [BLUE, COLOURS]),
        db.one("SELECT drex_decide_image('\"\"', ARRAY[%s, %s]::bytea[], %s) -> 'answers' -> 'colour' ->> 'choice'", [RED, BLUE, COLOURS]),
        db.one("SELECT drex_decide_image('\"\"', ARRAY[%s, %s]::bytea[], %s) -> 'answers' -> 'colour' ->> 'choice'", [BLUE, RED, COLOURS]),
    ]
    assert colours == ["blue", "red", "blue"]
    assert [r["model"] for r in db.requests()] == ["drex-max"] * 3
    assert [r["image_colours"] for r in db.requests()] == [["blue"], ["red", "blue"], ["blue", "red"]]


def test_images_with_a_text_model_fail_the_query(db: Variant) -> None:
    with pytest.raises(errors.InvalidParameterValue, match="drex-v2.0 does not read images; use an image model such as drex-max"):
        db.one("SELECT drex_decide_image('\"\"', ARRAY[%s::bytea], %s, model => 'drex-v2.0')", [RED, COLOURS])
    assert db.requests() == []


def test_a_bad_image_or_an_oversized_row_is_null_with_a_warning_and_the_statement_continues(db: Variant) -> None:
    results = db.conn.execute(
        """SELECT drex_decide_image('""', ARRAY[%s, 'GIF89a'::bytea], %s),
                  (drex_decide_batch(ARRAY['"a"', to_jsonb(repeat('x', 4200000)), '"b"']::jsonb[], %s))""",
        [RED, COLOURS, NOUL],
    ).fetchone()
    assert results[0] is None
    assert [result is None for result in results[1]] == [False, True, False]
    assert db.warnings[0] == "drex_decide_image: a row returned NULL: image 2 is not a PNG, JPEG or WebP image"
    assert db.warnings[1].startswith("drex_decide_batch: row 2 returned NULL: the request body is ")
    assert db.warnings[1].endswith(f"over Drex's limit of {4 * 1024 * 1024}")
    assert len(db.requests()) == 2


def test_a_row_over_the_token_limit_is_null_with_a_warning(db: Variant) -> None:
    db.use("too-long")
    assert db.one("SELECT drex_noul(repeat('x', 10000), 'Refund?')") is None
    [warning] = db.warnings
    assert warning.startswith("drex_noul: a row returned NULL: HTTP 422: state: exceeds the 1,024-token state limit")


def test_checks_fail_the_query_with_invalid_parameter_value(db: Variant) -> None:
    cases = [
        ("SELECT drex_choice('x', 'Which?', '[\"only\"]')", "drex_choice: choice takes 2 to 255 labels, got 1"),
        ("SELECT drex_score('x', 'How?', '[{\"label\": \"lo\", \"description\": \"d\"}, \"hi\"]')", "score levels take no description"),
        ("SELECT drex_noul('x', 'Q?', '[\"yes\", \"no\"]')", r"noul labels must be exactly true and false, got \[yes, no\]"),
        ("SELECT drex_noul('x', '')", "drex_noul: noul needs instructions, or a description of true or false"),
        ("SELECT drex_decide('\"x\"', '{\"__proto__\": {\"type\": \"noul\", \"instructions\": \"q\"}}')", "'__proto__' is not allowed"),
        ("SELECT drex_decide('\"x\"', '{\"a\": {\"type\": \"box\"}}')", "questions.a: box questions are not supported"),
        ("SELECT drex_noul('x', 'Refund?', model => 'drex-v9')", "unknown model 'drex-v9'; GET /v1/models lists drex-v1.5"),
        (f"SELECT drex_decide_batch(ARRAY['\"x\"']::jsonb[], '{NOUL}', concurrency => 0)", "concurrency must be at least 1"),
    ]
    for sql, message in cases:
        with pytest.raises(errors.InvalidParameterValue, match=message):
            db.conn.execute(sql)
    assert db.requests() == []


def test_null_inputs_return_null_and_null_model_is_the_default(db: Variant) -> None:
    assert db.one("SELECT drex_noul(NULL::text, 'Refund?')") is None
    assert db.one("SELECT drex_decide_image('\"\"', ARRAY[NULL]::bytea[], %s)", [COLOURS]) is None
    db.one("SELECT drex_noul('x', 'Refund?', model => NULL)")
    db.one("SELECT drex_decide_image('\"\"', ARRAY[%s::bytea], %s, NULL)", [RED, COLOURS])
    assert [r["model"] for r in db.requests()] == ["drex-v2.0", "drex-max"]


def test_401_and_402_fail_the_query(db: Variant) -> None:
    db.use("401")
    with pytest.raises(errors.InvalidAuthorizationSpecification, match="drex_noul: Invalid API key"):
        db.one("SELECT drex_noul('x', 'Refund?')")
    db.use("402")
    with pytest.raises(errors.InsufficientResources, match=f"insufficient available funds.*Billing: {INSIDE}/ns/{db.ns}/402/dashboard/billing"):
        db.one("SELECT drex_noul('x', 'Refund?')")


def test_a_422_about_the_query_fails_it_with_every_issue(db: Variant) -> None:
    db.use("422")
    with pytest.raises(errors.InvalidParameterValue, match="questions.a.instructions: must be a string; questions.a.instructions: noul needs"):
        db.one("SELECT drex_noul('x', 'Refund?')")


def test_429_is_retried_after_retry_after_ms(db: Variant) -> None:
    db.use("429-once")
    assert db.one("SELECT drex_noul(%s, 'Is the customer asking for a refund?')", [TICKET]) == 0.9868
    first, second = db.requests()
    assert (first["status"], second["status"]) == (429, 200)
    assert second["waited_ms"] >= 1000 and first["idempotency_key"] == second["idempotency_key"]


def test_a_cancel_or_statement_timeout_stops_a_waiting_call(db: Variant) -> None:
    db.use("slow-5000")
    db.conn.execute("SET statement_timeout = 600")
    started = time.monotonic()
    with pytest.raises(errors.QueryCanceled):
        db.one("SELECT drex_noul('x', 'Refund?')")
    assert time.monotonic() - started < 2
    db.conn.execute("RESET statement_timeout")
    # The session still works
    db.use()
    assert db.one("SELECT drex_noul('x', 'Refund?')") == 0.75


def test_drex_decide_batch_keeps_the_order_and_runs_with_unnest(db: Variant) -> None:
    db.conn.execute("CREATE TEMP TABLE tickets AS SELECT i AS id, to_jsonb('ticket ' || i) AS body FROM generate_series(1, 12) i")
    rows = db.conn.execute(
        """WITH batch AS (SELECT array_agg(id ORDER BY id) AS ids, array_agg(body ORDER BY id) AS states FROM tickets)
           SELECT batch.ids[r.n], r.answer -> 'answers' -> 'a' ->> 'noul'
           FROM batch, unnest(drex_decide_batch(batch.states, %s, concurrency => 4)) WITH ORDINALITY AS r(answer, n)""",
        [NOUL],
    ).fetchall()
    assert [row[0] for row in rows] == list(range(1, 13)) and {row[1] for row in rows} == {"0.9868"}
    sent = sorted(json.loads(r["state"]) for r in db.requests())
    assert sent == sorted(f"ticket {i}" for i in range(1, 13))


def test_drex_decide_batch_concurrency(db: Variant) -> None:
    db.use("slow-150")
    db.one("SELECT drex_decide_batch(ARRAY(SELECT to_jsonb(i) FROM generate_series(1, 12) i), %s, concurrency => 4)", [NOUL])
    peak = max(r["in_flight"] for r in db.requests())
    # The pgsql-http variant sends one request at a time
    assert peak == (4 if db.schema == "drex_py" else 1)


def test_functions_are_revoked_from_public_and_the_key_is_unreadable(db: Variant) -> None:
    analyst = psycopg.conninfo.make_conninfo(DSN, user="drex_analyst", password="analyst")
    with psycopg.connect(analyst, autocommit=True) as conn:
        conn.execute(f"SET search_path = {db.schema}")
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute("SELECT drex_noul('x', 'Refund?')")
        db.conn.execute("GRANT EXECUTE ON FUNCTION drex_noul(text, text, jsonb, text) TO drex_analyst")
        try:
            assert conn.execute("SELECT drex_noul('x', 'Refund?')").fetchone()[0] == 0.75
            if db.schema == "drex_http":
                with pytest.raises(errors.InsufficientPrivilege):
                    conn.execute("SELECT api_key FROM drex_settings")
                with pytest.raises(errors.InsufficientPrivilege):
                    conn.execute("SELECT _drex_settings()")
        finally:
            db.conn.execute("REVOKE EXECUTE ON FUNCTION drex_noul(text, text, jsonb, text) FROM drex_analyst")


def test_a_choice_reaches_drex_in_the_order_written(db: Variant) -> None:
    labels = ["technical", "billing", "account"]
    db.one("SELECT drex_choice('x', 'Which team?', %s)", [json.dumps(labels)])
    db.one("SELECT drex_decide('\"x\"', %s)", [json.dumps({"team": {"type": "choice", "instructions": "Which team?", "criteria": labels}})])
    sent = [next(iter(json.loads(r["body"])["questions"].values())) for r in db.requests()]
    assert [list(question["criteria"]) for question in sent] == [labels, labels]


def test_a_multidimensional_array_fails_the_query(db: Variant) -> None:
    with pytest.raises(errors.InvalidParameterValue, match="drex_decide_batch: states must be a one-dimensional array"):
        db.one("SELECT drex_decide_batch(ARRAY[['\"a\"', '\"b\"'], ['\"c\"', '\"d\"']]::jsonb[], %s)", [NOUL])
    with pytest.raises(errors.InvalidParameterValue, match="drex_decide_image: images must be a one-dimensional array"):
        db.one("SELECT drex_decide_image('\"x\"', ARRAY[[%s::bytea]], %s)", [RED, COLOURS])
    assert db.requests() == []


def test_a_caller_who_cannot_read_the_key_cannot_loosen_tls(db: Variant) -> None:
    """Any role may call pgsql-http's http_set_curlopt, whose options would apply to the request carrying the key"""
    if db.schema == "drex_py":
        pytest.skip("the PL/Python variant doesn't send through pgsql-http")
    analyst = psycopg.conninfo.make_conninfo(DSN, user="drex_analyst", password="analyst")
    db.conn.execute("GRANT EXECUTE ON FUNCTION drex_noul(text, text, jsonb, text) TO drex_analyst")
    try:
        with psycopg.connect(analyst, autocommit=True) as conn:
            conn.execute(f"SET search_path = {db.schema}, public")
            conn.execute("SELECT http_set_curlopt('CURLOPT_TIMEOUT_MS', '20000')")
            assert conn.execute("SELECT drex_noul('x', 'Refund?')").fetchone()[0] == 0.75
            conn.execute("SELECT http_set_curlopt('CURLOPT_SSL_VERIFYPEER', '0')")
            with pytest.raises(errors.InsufficientPrivilege, match=r"\(CURLOPT_SSL_VERIFYPEER\); call http_reset_curlopt\(\) first"):
                conn.execute("SELECT drex_noul('y', 'Refund?')")
            conn.execute("SELECT http_reset_curlopt()")
            assert conn.execute("SELECT drex_noul('y', 'Refund?')").fetchone()[0] == 0.75
    finally:
        db.conn.execute("REVOKE EXECUTE ON FUNCTION drex_noul(text, text, jsonb, text) FROM drex_analyst")
    # The refused call sent nothing
    assert len(db.requests()) == 2


def test_the_http_variant_installs_as_a_non_superuser_without_default_grants(installed: None) -> None:
    """As on Supabase, whose postgres role isn't a superuser, and whose default privileges grant every new table and
    function to anon and authenticated (here, drex_analyst)"""
    owner = psycopg.conninfo.make_conninfo(DSN, user="drex_owner", password="owner")
    script = (ROOT / "sql" / "drex_http.sql").read_text()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS drex_owned CASCADE")
        if conn.execute("SELECT FROM pg_roles WHERE rolname = 'drex_owner'").fetchone():
            conn.execute("DROP OWNED BY drex_owner; DROP ROLE drex_owner")
        conn.execute("CREATE ROLE drex_owner LOGIN PASSWORD 'owner'; CREATE SCHEMA drex_owned AUTHORIZATION drex_owner")
        for kind in ("TABLES", "FUNCTIONS"):
            conn.execute(f"ALTER DEFAULT PRIVILEGES FOR ROLE drex_owner IN SCHEMA drex_owned GRANT ALL ON {kind} TO drex_analyst")
    try:
        # A new connection, which hasn't loaded pgsql-http
        with psycopg.connect(owner, autocommit=True) as conn:
            conn.execute("SET search_path = drex_owned")
            conn.execute(script)
            conn.execute("INSERT INTO drex_settings (api_key, base_url) VALUES (%s, %s)", [MOCK_KEY, f"{INSIDE}/ns/owned/ok"])
        with psycopg.connect(owner, autocommit=True) as conn:
            assert conn.execute("SELECT drex_owned.drex_noul('x', 'Refund?')").fetchone()[0] == 0.75
        privileges = """SELECT has_table_privilege('drex_analyst', 'drex_owned.drex_settings', 'SELECT'),
                               array_agg(p.proname ORDER BY p.proname) FILTER (WHERE has_function_privilege('drex_analyst', p.oid, 'EXECUTE'))
                        FROM pg_proc p WHERE p.pronamespace = 'drex_owned'::regnamespace"""
        with psycopg.connect(DSN) as conn:
            assert conn.execute(privileges).fetchone() == (False, None)
        # A grant made since is kept by a reinstall
        with psycopg.connect(owner, autocommit=True) as conn:
            conn.execute("GRANT EXECUTE ON FUNCTION drex_owned.drex_noul(text, text, jsonb, text) TO drex_analyst")
            conn.execute("SET search_path = drex_owned")
            conn.execute(script)
        with psycopg.connect(DSN) as conn:
            assert conn.execute(privileges).fetchone() == (False, ["drex_noul"])
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP SCHEMA drex_owned CASCADE; DROP OWNED BY drex_owner; DROP ROLE drex_owner")


def test_the_plpython_variant_installs_without_default_grants(installed: None) -> None:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS drex_granted CASCADE; CREATE SCHEMA drex_granted")
        conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA drex_granted GRANT ALL ON FUNCTIONS TO drex_analyst")
        try:
            conn.execute("SET search_path = drex_granted")
            conn.execute((ROOT / "sql" / "drex.sql").read_text())
            assert conn.execute(
                """SELECT count(*) FROM pg_proc p WHERE p.pronamespace = 'drex_granted'::regnamespace
                   AND has_function_privilege('drex_analyst', p.oid, 'EXECUTE')"""
            ).fetchone()[0] == 0
        finally:
            conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA drex_granted REVOKE ALL ON FUNCTIONS FROM drex_analyst")
            conn.execute("DROP SCHEMA drex_granted CASCADE")


@pytest.mark.parametrize("script", ["drex.sql", "drex_http.sql"])
@pytest.mark.parametrize("schema", ["Drex Funcs", "DrexFuncs"])
def test_either_variant_installs_into_a_schema_whose_name_needs_quoting(installed: None, script: str, schema: str) -> None:
    quoted = psycopg.sql.Identifier(schema)
    try:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {0} CASCADE; CREATE SCHEMA {0}").format(quoted))
            conn.execute(psycopg.sql.SQL("SET search_path = {}").format(quoted))
            conn.execute((ROOT / "sql" / script).read_text())
            if script == "drex_http.sql":
                conn.execute("INSERT INTO drex_settings (api_key, base_url) VALUES (%s, %s)", [MOCK_KEY, f"{INSIDE}/ns/quoted/ok"])
            assert conn.execute(psycopg.sql.SQL("SELECT {}.drex_noul('x', 'Refund?')").format(quoted)).fetchone()[0] == 0.75
    finally:
        # A new connection: a failed install leaves its own in an aborted transaction
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(quoted))


def test_an_http_install_into_another_schema_leaves_the_first_working() -> None:
    """The first install shares its schema with the http extension, as when both are in public, so that schema is on
    the second install's search_path. In a database of its own, since http can't move schemas."""
    script = (ROOT / "sql" / "drex_http.sql").read_text()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS drex_two_installs WITH (FORCE)")
        conn.execute("CREATE DATABASE drex_two_installs")
    try:
        with psycopg.connect(psycopg.conninfo.make_conninfo(DSN, dbname="drex_two_installs"), autocommit=True) as conn:
            conn.execute("CREATE SCHEMA drex_first; CREATE SCHEMA drex_second; CREATE EXTENSION http SCHEMA drex_first")
            conn.execute("SET search_path = drex_first")
            conn.execute(script)
            conn.execute("INSERT INTO drex_settings (api_key, base_url) VALUES (%s, %s)", [MOCK_KEY, f"{INSIDE}/ns/first/ok"])
            conn.execute("SET search_path = drex_second")
            conn.execute(script)
            assert conn.execute("SELECT drex_first.drex_noul('x', 'Refund?')").fetchone()[0] == 0.75
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP DATABASE drex_two_installs WITH (FORCE)")


def test_a_cleartext_base_url_is_refused(db: Variant) -> None:
    db.use(base_url="http://drex.example.com")
    with pytest.raises(errors.InvalidAuthorizationSpecification, match="must be an https:// URL"):
        db.one("SELECT drex_noul('x', 'Refund?')")


OPEN_ROUTER_KEY = "sk-or-v1-" + "o" * 40
SETTING_NAMES = {
    "api_key": ("NACE_API_KEY", "api_key"),
    "openrouter_api_key": ("OPENROUTER_API_KEY", "openrouter_api_key"),
    "referer": ("NACE_OPENROUTER_REFERER", "openrouter_referer"),
    "title": ("NACE_OPENROUTER_TITLE", "openrouter_title"),
}


def setting(db: Variant, name: str, value: str | None) -> None:
    """A setting as the variant keeps it: the server's environment for PL/Python, a drex_settings column otherwise."""
    env_name, column = SETTING_NAMES[name]
    if db.schema == "drex_py":
        db.conn.execute("SELECT drex_test.setenv(%s, %s)", [env_name, value])
    else:
        db.conn.execute(f"UPDATE drex_settings SET {column} = %s", [value])


@pytest.fixture
def open_router(db: Variant) -> Iterator[Variant]:
    """https://openrouter.ai/api through the mock, which answers as OpenRouter does, with its own certificate."""
    pem = urllib.request.urlopen(f"{MOCK}/__fixtures/openrouter.pem").read().decode()
    db.conn.execute("SELECT drex_test.write_file('/tmp/openrouter.pem', %s)", [pem])
    if db.schema == "drex_py":
        for name, value in (("HTTPS_PROXY", INSIDE), ("SSL_CERT_FILE", "/tmp/openrouter.pem")):
            db.conn.execute("SELECT drex_test.setenv(%s, %s)", [name, value])
    else:
        db.conn.execute(f"SET http.curlopt_proxy = '{INSIDE}'; SET http.curlopt_cainfo = '/tmp/openrouter.pem'")
    setting(db, "api_key", OPEN_ROUTER_KEY)
    db.mock.reset("default")
    try:
        db.use(base_url="https://openrouter.ai/api")
        yield db
    finally:
        if db.schema == "drex_py":
            for name in ("HTTPS_PROXY", "SSL_CERT_FILE"):
                db.conn.execute("SELECT drex_test.setenv(%s, NULL)", [name])
        for name in ("openrouter_api_key", "referer", "title"):
            setting(db, name, None)
        setting(db, "api_key", MOCK_KEY)


def test_open_router(open_router: Variant) -> None:
    db = open_router
    assert db.one("SELECT drex_noul(%s, 'Is the customer asking for a refund?')", [TICKET]) == 0.9868
    body = db.one("SELECT drex_decide_image('\"look\"', ARRAY[%s, %s]::bytea[], %s)", [RED, BLUE, COLOURS])
    assert body["answers"]["colour"]["choice"] == "red"
    _, text, image = db.mock.log("default")
    assert [text["model"], image["model"]] == ["nace-ai/drex-v2.0", "nace-ai/drex-max"]
    assert image["state_type"] == "array" and image["image_colours"] == ["red", "blue"] and '"images"' not in image["body"]
    assert {(r["http_referer"], r["x_openrouter_title"]) for r in (text, image)} == {("https://nace.ai", "Drex for PostgreSQL")}
    assert {r["authorization_header"] for r in (text, image)} == {f"Bearer {OPEN_ROUTER_KEY}"}


def test_open_router_attribution_is_set_by_the_caller(open_router: Variant) -> None:
    setting(open_router, "referer", "https://app.example")
    setting(open_router, "title", "Example")
    assert open_router.one("SELECT drex_noul(%s, 'Is the customer asking for a refund?')", [TICKET]) == 0.9868
    sent = open_router.mock.log("default")
    assert [r["endpoint"] for r in sent] == ["/v1/models", "/v1/systemone"]
    assert {(r["http_referer"], r["x_openrouter_title"]) for r in sent} == {("https://app.example", "Example")}


def test_a_nace_host_gets_no_attribution_headers(db: Variant) -> None:
    setting(db, "referer", "https://app.example")
    setting(db, "title", "Example")
    try:
        assert db.one("SELECT drex_noul(%s, 'Is the customer asking for a refund?')", [TICKET]) == 0.9868
        assert {(r["http_referer"], r["x_openrouter_title"]) for r in db.requests()} == {(None, None)}
    finally:
        setting(db, "referer", None)
        setting(db, "title", None)


def test_a_nace_key_is_never_sent_to_open_router(open_router: Variant) -> None:
    setting(open_router, "api_key", MOCK_KEY)
    with pytest.raises(errors.InvalidAuthorizationSpecification, match="never sent to OpenRouter"):
        open_router.one("SELECT drex_noul(%s, 'Is the customer asking for a refund?')", [TICKET])
    assert open_router.mock.log("default") == []


def test_open_router_prefers_its_own_key(open_router: Variant) -> None:
    setting(open_router, "api_key", MOCK_KEY)
    setting(open_router, "openrouter_api_key", OPEN_ROUTER_KEY)
    assert open_router.one("SELECT drex_noul(%s, 'Is the customer asking for a refund?')", [TICKET]) == 0.9868
    assert {r["authorization_header"] for r in open_router.mock.log("default")} == {f"Bearer {OPEN_ROUTER_KEY}"}
