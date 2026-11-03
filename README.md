<p align="center">
  <a href="https://nace.ai"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/nace-logo-white.svg"><img src="docs/assets/nace-logo-black.svg" alt="Nace" height="40"></picture></a>
</p>

<h1 align="center">Drex for PostgreSQL®</h1>

<p align="center"><b>Classify text, score rows and ask structured questions from PostgreSQL.</b></p>

<p align="center">
  <a href="https://github.com/nace-ai/drex-postgres/actions/workflows/MockContractTests.yml"><img src="https://img.shields.io/badge/tests-GitHub_Actions-2088FF" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
</p>

Ask typed questions about your rows directly from SQL. These functions connect [PostgreSQL](https://www.postgresql.org/) to
[Drex](https://console.nace.ai/docs), Nace's decision models, to classify text, score rows and return calibrated
probabilities as SQL values.

[Which variant](#which-variant) · [Install](#install) · [Examples](#examples) · [API reference](docs/API.md) ·
[Development](#development)

## What it does

| Function | Result | Use case |
| --- | --- | --- |
| `drex_noul` | `double precision` probability of yes | Flag refund requests |
| `drex_choice` | `jsonb`: label, probabilities, confidence | Route tickets to a team |
| `drex_score` | `jsonb`: score, probabilities, confidence | Rank urgency on an ordered scale |
| `drex_decide` | `jsonb` full response | Ask several questions per row |
| `drex_decide_image` | `jsonb` response about image bytes | Classify photos or receipts |

## Which variant

There are two, with the same function names, arguments and results:

| | `sql/drex.sql` (PL/Python) | `sql/drex_http.sql` (pgsql-http) |
| --- | --- | --- |
| Needs | `plpython3u` and the `drex-postgres` Python package on the server | the [`http`](https://github.com/pramsey/pgsql-http) extension, 1.6 or later |
| Where | self-hosted Postgres, where you can install packages into the server's Python | managed Postgres that offers `http` but not `plpython3u` |
| API key | `NACE_API_KEY` in the server's environment | the `drex_settings` table, readable only by its owner |
| Requests | `drex_decide_batch` sends up to `concurrency` at once | one at a time |
| Built on | [`nace-sdk`](https://pypi.org/project/nace-sdk/) | PL/pgSQL |

What the managed services checked offer, on 2026-10-30:

| Service | `plpython3u` | `http` | So |
| --- | --- | --- | --- |
| Amazon RDS for PostgreSQL, Aurora PostgreSQL | No | No | Neither variant. RDS and Aurora offer `aws_lambda` instead. |
| Google Cloud SQL for PostgreSQL | No | No | Neither variant. Cloud SQL installs only the extensions it lists. |
| Supabase | No | Yes (`create extension http with schema extensions`) | `sql/drex_http.sql` |
| Neon | No | No | Neither variant. |

`plpython3u` is an untrusted language, so it needs a superuser, which none of these services gives you. Sources: the
extension lists of [RDS](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html),
[Aurora](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraPostgreSQLReleaseNotes/AuroraPostgreSQL.Extensions.html),
[Cloud SQL](https://docs.cloud.google.com/sql/docs/postgres/extensions),
[Supabase](https://supabase.com/docs/guides/database/extensions/http) and
[Neon](https://neon.com/docs/extensions/pg-extensions). Lists change: check yours before you install.

## Install

Clone this repository first; all paths below are relative to its root. The Python package is installed from
source, not PyPI. Use psql 15+ for the `\getenv` command below.

```sh
git clone --branch v0.3.1 https://github.com/nace-ai/drex-postgres.git
cd drex-postgres
```

`v0.3.1` is the latest release; [CHANGELOG.md](CHANGELOG.md) lists each release's changes. To upgrade, check out the
new release, reinstall the package (PL/Python variant), and run the same script again: it replaces the functions and
keeps `drex_settings` and your grants. Sessions opened afterwards use the new version.

Both scripts create their functions in the first schema on `search_path`, revoke them from `PUBLIC`, and run in one
transaction. Install one variant, then grant access as described below.

### PL/Python (self-hosted)

1. Install PL/Python for your Postgres major version, for example `apt-get install postgresql-plpython3-17`.
2. Install this package into the Python that `plpython3u` runs, which is the system Python on most distributions:

   ```sh
   sudo pip3 install --break-system-packages .
   ```

   It needs Python 3.10 or later, so not a `plpython3u` built against RHEL 9's Python 3.9. After
   `CREATE EXTENSION plpython3u`, `DO $$ import sys; plpy.notice(sys.version) $$ LANGUAGE plpython3u;` shows the
   version it runs. pip older than 23 has no `--break-system-packages`; leave it out there.

3. Set the key where the server starts, so it is in every backend's environment, and restart Postgres.
   With systemd, use a root-owned environment file with mode `0600`, outside this checkout. Edit that file
   locally to contain `NACE_API_KEY=YOUR_API_KEY`, then reference it in the service override:

   ```ini
   # systemctl edit postgresql@17-main
   [Service]
   EnvironmentFile=/etc/postgresql/drex.env
   ```

   `NACE_BASE_URL` optionally names another endpoint: `https://` only, except `localhost`, `127.0.0.1` and `[::1]`.
   The key is never a function argument or a setting, since any user can read those.

4. Run the script as a superuser:

   ```sh
   psql -v ON_ERROR_STOP=1 -c 'SET search_path = public' -f sql/drex.sql
   ```

   It fails at once if `plpython3u` can't import `drex_postgres`.

### pgsql-http (Supabase and other managed Postgres)

1. Enable the extension: on Supabase, `CREATE EXTENSION http WITH SCHEMA extensions;` (or from the dashboard);
   elsewhere, `CREATE EXTENSION http;`.
2. Run the script as the role that will own the functions and the key, with the target schema first on
   `search_path`. It pins every function's `search_path` to that schema, the `http` extension's, and `pg_temp`:

   ```sh
   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c 'SET search_path = public' -f sql/drex_http.sql
   ```

3. Store the key without typing it into SQL:

   ```sql
   \getenv key NACE_API_KEY
   INSERT INTO drex_settings (api_key) VALUES (:'key');
   ```

   With `log_statement = 'all'` (or `'mod'`) the server log records that statement; turn it off for the session
   first if you can. `UPDATE drex_settings SET api_key = :'key';` replaces the key, and
   `UPDATE drex_settings SET base_url = ...` names another endpoint.

The functions run as their owner (`SECURITY DEFINER`) to read the key, so anyone granted them spends it, and no one
else can read it. Supabase grants every new table and function in `public` to `anon`, `authenticated` and
`service_role`; the script revokes those grants from what it creates, so only the roles you grant can call the
functions. Functions granted to `anon` or `authenticated` in a schema Supabase's Data API exposes can be called with
your project's API keys.

### Grant access after installation

Grant execution only to roles allowed to spend the key. Replace `analyst` with an existing role and `public`
with your installation schema. This example grants every function in that schema; use individual function
signatures if the schema contains other functions.

```sql
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO analyst;
```

## Examples

After installing either variant, run this synthetic fixture in the same database session as the examples:

```sql
CREATE TEMP TABLE tickets (id integer, body text);
INSERT INTO tickets VALUES
    (1, 'I was charged twice. Please refund the duplicate payment.'),
    (2, 'How do I change my email address?');

SELECT id, drex_noul(body, 'Is the customer asking for a refund?') AS refund_probability
FROM tickets;
```

The result is a probability from 0 to 1, or NULL when no answer is available. Model answers can vary.

```sql
-- Which team should handle each ticket?
SELECT id, drex_choice(body, 'Which team should handle this ticket?', '["billing", "technical", "account"]') AS team
FROM tickets;

-- Rate on an ordered scale, with Drex Max
SELECT id, drex_score(body, 'How urgent is this ticket?', '["low", "medium", "high"]', model => 'drex-max') -> 'score'
FROM tickets;

-- Several questions about each row in one request
SELECT id, drex_decide(to_jsonb(body), '{
  "refund":  {"type": "noul",  "instructions": "Is the customer asking for a refund?"},
  "urgency": {"type": "score", "instructions": "How urgent is this ticket?", "criteria": ["low", "medium", "high"]}
}') -> 'answers'
FROM tickets;

-- Images, with Drex Max. Requires a real PNG at /srv/receipts/1.png on the database server
-- and permission to call pg_read_binary_file; unavailable to most managed-service roles.
SELECT drex_decide_image('"Receipt photo"', ARRAY[pg_read_binary_file('/srv/receipts/1.png')],
                         '{"receipt": {"type": "noul", "instructions": "Is this a receipt?"}}');
```

### Many rows: `drex_decide_batch`

The scalar functions send one request per row, one at a time, since a Postgres backend runs one function call at a
time. `drex_decide_batch` sends a chunk of rows at once (PL/Python variant), and returns one result per state, in
order:

```sql
CREATE TEMP TABLE ticket_answers (id integer, answer jsonb);

WITH chunk AS (
  SELECT array_agg(id ORDER BY id) AS ids, array_agg(to_jsonb(body) ORDER BY id) AS states
  FROM tickets WHERE id BETWEEN 1 AND 500
)
INSERT INTO ticket_answers (id, answer)
SELECT chunk.ids[r.n], r.answer
FROM chunk, unnest(drex_decide_batch(chunk.states,
    '{"refund": {"type": "noul", "instructions": "Is the customer asking for a refund?"}}',
    concurrency => 8)) WITH ORDINALITY AS r(answer, n);
```

`concurrency` (default 8) is per call, so the requests in flight are the number of sessions running it times their
`concurrency`. Drex limits each account's requests per minute and in flight; a request over a limit is retried after
the wait Drex asks for.

## Configuration and behavior

Text and JSON calls default to `drex-v2.0`; image calls default to `drex-max`. Set `model =>` to override it.
Invalid questions, authentication failures and unknown models fail the statement; row-specific failures return
NULL with a warning. Retryable failures get at most five attempts within 120 seconds.
See [the API reference](docs/API.md) for SQLSTATEs, cancellation and provider configuration.

## Requests, privacy and cost

The functions send each non-NULL row and the question's instructions to the configured API: use data you may send
there. Each row is normally one billed request, and only a `200` is billed. Retries can add attempts, and a statement
that runs again, or a query plan that evaluates a function for rows it later discards, sends those rows again: save
answers with `INSERT ... SELECT` or `CREATE TABLE ... AS` and filter rows before calling the functions.

A NULL result means the row has no answer (see [Errors](docs/API.md#errors)), never a negative one.

## Development

```sh
uv sync                                                       # .venv with the package and its test tools
uv run pytest                                                 # the functions against the mock; skips Postgres
uv run python -m unittest discover -s test/mock               # the mock's own checks

docker build -f docker/Dockerfile -t drex-postgres-test .     # Postgres 17, plpython3u, http and the mock
docker run -d --rm --name drex-pg -p 55432:5432 -p 58787:8787 drex-postgres-test
DREX_TEST_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres uv run pytest
```

`test/mock` is drex-duckdb's mock Drex API, vendored unchanged: it validates every request as Drex does and its
responses against Drex's `openapi.json`. `tests/test_postgres.py` runs every test on both variants. No test needs an
API key or calls Drex itself.

## Support and contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local checks and pull requests, and the
[API reference](docs/API.md) for the complete contract. Report bugs through
[GitHub Issues](https://github.com/nace-ai/drex-postgres/issues), with your runtime version and a minimal example
using synthetic data. Remove keys, signed URLs and private data from reports.

## License and trademarks

[MIT](LICENSE). Nace artwork belongs to Nace AI. The software license does not grant trademark rights.
See [asset sources and branding guidance](docs/assets/README.md).

This integration is maintained by Nace AI. References to the platform describe compatibility,
not sponsorship or endorsement by its owner.

PostgreSQL and Postgres are registered trademarks of the PostgreSQL Community Association of Canada.
