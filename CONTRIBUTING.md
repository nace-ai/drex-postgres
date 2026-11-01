# Contributing

Use [GitHub Issues](https://github.com/nace-ai/drex-postgres/issues) for bugs and proposals. Include the integration version,
runtime version, expected behavior and a small reproduction with synthetic inputs. Never include credentials,
signed image URLs or customer data.

## Local checks

Clone the repository and follow [Development](README.md#development) from the repository root.
The default tests use a local mock API. Database/container tests require the additional services listed there;
a passing mock test is not a live-platform check. See [.github/workflows](.github/workflows) for the CI commands.

Checks against the live Drex API need an API key and incur charges. Run them only with your own key and synthetic
data, and describe any you ran, or skipped, in the pull request.

## Pull requests

Keep the change focused, explain the behavior it changes and list the checks you actually ran. Update the
[API reference](docs/API.md) and examples if an interface changes. Preserve NULL/error semantics, credential
handling and request-cost warnings. Use synthetic fixtures and avoid committing generated bundles or local secrets.

## Releases

A release bumps the version in `pyproject.toml`, `__version__` in `src/drex_postgres/__init__.py` and
`_drex_client_name()` in `sql/drex_http.sql` together (`tests/test_version.py` checks they agree; the version is sent
as `X-Nace-Client: drex-postgres/<version>` and `drex-postgres-http/<version>`), adds its entry to
[CHANGELOG.md](CHANGELOG.md), updates the release named in the README's clone command, and is tagged
`vMAJOR.MINOR.PATCH` on `main` with a GitHub release.

## Branding

Follow the [branding guidance](docs/assets/README.md) before changing names or adding artwork.
An official download is not, by itself, permission to use a provider logo. Record sources and applicable usage terms.
