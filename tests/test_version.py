import re
from pathlib import Path

import tomllib

from drex_postgres import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_every_copy_of_the_version_agrees() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    [http_client] = re.findall(r"'drex-postgres-http/([^']+)'", (ROOT / "sql" / "drex_http.sql").read_text())
    assert __version__ == project == http_client
