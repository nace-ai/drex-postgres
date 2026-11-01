# SQL API

Both variants (`sql/drex.sql` and `sql/drex_http.sql`) define these functions, with the same arguments and results.
Install them as shown in the [README](../README.md#install). They expose the same SQL function signatures and
read criteria and questions as drex-duckdb's `prompt_drex` does.

| Function | Result |
| --- | --- |
| `drex_noul(input text \| jsonb, instructions text, criteria jsonb DEFAULT NULL, model text DEFAULT 'drex-v2.0')` | `double precision`: the probability that the answer is yes |
| `drex_choice(input text \| jsonb, instructions text, labels jsonb, model text DEFAULT 'drex-v2.0')` | `jsonb`: `{"choice", "probabilities": [{"value", "probability"}], "confidence"}`, the probabilities sorted by label |
| `drex_score(input text \| jsonb, instructions text, levels jsonb, model text DEFAULT 'drex-v2.0')` | `jsonb`: `{"score", "probabilities": [{"index", "value", "probability"}], "confidence"}`, the probabilities by index |
| `drex_decide(state jsonb, questions jsonb, model text DEFAULT 'drex-v2.0')` | `jsonb`: Drex's response body (`model`, `answers`, `usage`, `evaluation_time_ms`, `request_id`) |
| `drex_decide_image(state jsonb, images bytea[], questions jsonb, model text DEFAULT 'drex-max')` | `jsonb`: Drex's response body |
| `drex_decide_batch(states jsonb[], questions jsonb, model text DEFAULT 'drex-v2.0', concurrency integer DEFAULT 8)` | `jsonb[]`: one response body per state, in order |

Optional arguments can be named: `drex_noul(body, 'Refund?', model => 'drex-max')`. A NULL or empty `model` means
the default.

## Arguments

- **`input`**: `text` is sent as text. `jsonb` is sent as structured state. A literal like `'a ticket'` is `text`.
- **`instructions`**: NULL or `''` means none. A `noul` needs instructions, or a description of `true` or `false`.
- **`criteria`, `labels`, `levels`** take three JSON forms:
  - a list of labels, `["billing", "technical"]`
  - a list of `{"label", "description"}` objects, `[{"label": "billing", "description": "Payments and refunds"}]`
  - an object of label to description, `{"billing": "Payments and refunds", "other": null}` (not for `levels`)

  `noul` labels are exactly `true` and `false`; labels with no description are the same as no criteria. `choice`
  and `score` take 2 to 255 unique, non-empty labels. `levels` take no descriptions (describe the scale in
  `instructions`), and their order is the scale. A `jsonb` object doesn't keep its keys' order, so use a list when
  the order of `labels` matters to you.
- **`questions`**: an object of question id to `{"type", "instructions", "criteria"}`, with 1 to 512 questions.
  `type` is `noul`, `choice` or `score`; ids are non-empty and unique ignoring case; `__proto__` is refused, since
  Drex would bill the request and drop that answer. `criteria` takes the forms above.
- **`images`**: 1 to 8 PNG, JPEG or WebP images, read as bytes; Drex never fetches a URL. Inline images count toward
  Drex's 4 MiB request body, about 3 MiB of image bytes. A NULL image, or an empty array, returns NULL and sends
  nothing, and a row with more than 8 returns NULL with a warning. Only `drex-max` and `drex-max-v1.1` read images.
- **`states`** and **`images`** are one-dimensional arrays.
- **`model`** is checked against `GET /v1/models`, cached per session. On OpenRouter, a name without an owner is read
  as `nace-ai/<name>`.
- **`concurrency`**: at least 1. The pgsql-http variant sends one request at a time whatever it says.

A NULL `input`, `state`, `questions` or `states` returns NULL and sends nothing, after the other arguments are
checked.

## Errors

| Outcome | When |
| --- | --- |
| The statement fails, `22023` (`invalid_parameter_value`) | an argument breaks a rule above; an unknown model; images with a model that doesn't read them; a 422 from Drex about the questions or model, with each issue as `path: message` |
| The statement fails, `28000` (`invalid_authorization_specification`) | no key; a 401; a base URL that isn't `https://` (except on localhost) |
| The statement fails, `53000` (`insufficient_resources`) | a 402, with the billing page to add funds |
| The statement fails, `42501` (`insufficient_privilege`) | pgsql-http variant: the session set curl options a caller who can't read the key may not set (see [Clients](#clients)) |
| The statement fails, `38000` (`external_routine_exception`) | a TLS failure; Drex couldn't list its models or answered with an unexpected body |
| NULL, with a `WARNING` | a 422 about the row itself (`Request too long:`, or issues only under `state` or `images`); a body over 4 MiB; an image that isn't a PNG, JPEG or WebP; retries running out; any other status |

Warnings never quote the row. A NULL is a missing answer, not a negative one.

No response, 408, 429, 500, 502, 503, 504 and 529 are retried, up to 5 attempts within 120 seconds of the first,
waiting for `retry-after-ms` (or `retry-after`) up to 30 seconds, else 1, 2, 4 and 8 seconds less up to a quarter.
Every attempt sends the same body and `Idempotency-Key`. Drex bills only a `200`, so an attempt is billed twice only
when a `200` was lost on its way back. A cancel or `statement_timeout` stops a call within a fraction of a second; in
the PL/Python variant, requests already in flight finish in the background.

## Clients

Requests carry `User-Agent` and `X-Nace-Client`: `drex-postgres/<version>` from the PL/Python variant,
`drex-postgres-http/<version>` from the pgsql-http variant.

The pgsql-http variant sends with the session's curl options, which any role can set with `http_set_curlopt` (and, in
pgsql-http 1.7, the `http.curlopt_*` settings). For a caller who can't read `drex_settings`, only timeouts, keepalive,
IP resolution, the user agent and, to an `https://` base URL, a proxy or DNS servers are allowed; any other option, such
as one that changes how TLS checks the server, fails the call before it sends the key. `http_reset_curlopt()` clears
them. Roles that can read `drex_settings`, the owner and superusers among them, may set any option, for example a
custom CA. With `NACE_BASE_URL` (or `drex_settings.base_url`) at
`https://openrouter.ai/api` and an OpenRouter key, requests take OpenRouter's form and carry its attribution headers.

On OpenRouter the attribution headers are `HTTP-Referer: https://nace.ai` and `X-OpenRouter-Title: Drex for PostgreSQL`,
sent only to OpenRouter. To report your own app instead, set `NACE_OPENROUTER_REFERER` and `NACE_OPENROUTER_TITLE` in the
server's environment (PL/Python), or `drex_settings.openrouter_referer` and `drex_settings.openrouter_title`
(pgsql-http). The key is `OPENROUTER_API_KEY` (pgsql-http: `drex_settings.openrouter_api_key`) when set, else the Nace
key setting, which may hold an `sk-or-` key. A `nace_` key is refused before any request.
