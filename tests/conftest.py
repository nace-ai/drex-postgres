"""The mock Drex API (test/mock/drex_mock.py, vendored from drex-duckdb) and helpers shared by the tests."""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

MOCK_DIR = Path(__file__).resolve().parent.parent / "test" / "mock"
sys.path.insert(0, str(MOCK_DIR))
import drex_mock  # noqa: E402
import pngs  # noqa: E402

#: A well-formed test key, never a real one
MOCK_KEY = "nace_sk_" + "m" * 43
RED = pngs.fixture("red")
BLUE = pngs.fixture("blue")


@dataclass
class Mock:
    url: str

    def log(self, ns: str) -> list[dict]:
        with urllib.request.urlopen(f"{self.url}/__log/{ns}") as response:
            records = [json.loads(line) for line in response.read().splitlines()]
        return [record for record in records if record["endpoint"] != "(log)"]

    def requests(self, ns: str) -> list[dict]:
        """The namespace's POST /v1/systemone requests."""
        return [record for record in self.log(ns) if record["endpoint"] == "/v1/systemone"]

    def reset(self, ns: str) -> None:
        urllib.request.urlopen(f"{self.url}/__reset/{ns}").read()


@pytest.fixture(scope="session")
def mock() -> Iterator[Mock]:
    server = drex_mock.make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Mock(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()


@dataclass
class Host:
    """drex_postgres.functions.Host that keeps its warnings."""

    warnings: list[str] = field(default_factory=list)
    polls: int = 0

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def poll(self) -> None:
        self.polls += 1
