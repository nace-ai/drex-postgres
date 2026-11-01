"""The entry point of the PL/Python functions in sql/drex.sql: `call(plpy, "drex_noul", ...)`."""

from __future__ import annotations

from typing import Any

from drex_postgres import functions
from drex_postgres.errors import DrexError


def call(plpy: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Runs a function of drex_postgres.functions, raising each DrexError as a Postgres error with its SQLSTATE."""
    host = functions.Host(
        warning=plpy.warning,
        # Any statement runs CHECK_FOR_INTERRUPTS, so this raises once the statement is cancelled or times out. Only
        # this thread calls plpy: worker threads never do
        poll=lambda: plpy.execute("SELECT 1"),
    )
    try:
        return getattr(functions, name)(host, *args, **kwargs)
    except DrexError as error:
        plpy.error(str(error), sqlstate=error.sqlstate)
