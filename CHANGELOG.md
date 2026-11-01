# Changelog

## 0.3.0 (2026-11-01)

The first public release.

- Nine functions in two variants with the same names, arguments and results: `drex_noul`, `drex_choice` and
  `drex_score` (each for `text` and `jsonb` input), `drex_decide`, `drex_decide_image` and `drex_decide_batch`.
- `sql/drex.sql`, for self-hosted PostgreSQL: PL/Python on [`nace-sdk`](https://pypi.org/project/nace-sdk/), with the
  key in the server's environment and `drex_decide_batch` sending up to `concurrency` requests at once.
- `sql/drex_http.sql`, for managed PostgreSQL that offers the [`http`](https://github.com/pramsey/pgsql-http)
  extension, such as Supabase: PL/pgSQL, with the key in a `drex_settings` table only its owner can read. It installs
  as a role that isn't a superuser, and a caller who can't read the key can't loosen how its requests check TLS.
- Both scripts revoke the functions from `PUBLIC` and from roles that default privileges grant new objects to, such
  as Supabase's `anon` and `authenticated`.
- Optional OpenRouter routing, with its own key and app attribution.
- Requests identify themselves as `drex-postgres/0.3.0` and `drex-postgres-http/0.3.0` in `X-Nace-Client`.
