"""The errors that fail a query, each with the SQLSTATE Postgres reports it under."""


class DrexError(Exception):
    """Fails the query: Drex can't be reached or answered unexpectedly."""

    #: external_routine_exception
    sqlstate = "38000"


class DrexInputError(DrexError):
    """Fails the query before anything is sent, or Drex refused the query's constants (a 422 not about the row)."""

    #: invalid_parameter_value
    sqlstate = "22023"


class DrexAuthError(DrexError):
    """The API key is missing or was refused (401)."""

    #: invalid_authorization_specification
    sqlstate = "28000"


class DrexCreditError(DrexError):
    """The account has no funds left (402)."""

    #: insufficient_resources
    sqlstate = "53000"
