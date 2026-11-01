"""What each SQL function does, independent of PL/Python so that it can be tested without Postgres.

Every function takes a `Host`: how it warns (`plpy.warning` in Postgres) and how it lets Postgres check for a
cancel while it waits (`poll`). jsonb arguments arrive as JSON text, and jsonb results leave as JSON text.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from drex_postgres import client
from drex_postgres.errors import DrexInputError
from drex_postgres.questions import (
    Question,
    answer_value,
    named_questions,
    parse_json,
    questions_to_drex,
    typed_question,
)

DEFAULT_MODEL = "drex-v2.0"
DEFAULT_IMAGE_MODEL = "drex-max"
DEFAULT_CONCURRENCY = 8


@dataclass
class Host:
    warning: Callable[[str], None]
    poll: Callable[[], None]


def _model(model: str | None, default: str) -> str:
    return model if model else default


def _send(
    host: Host,
    function: str,
    model: str,
    requests: Sequence[client.Request | None],
    concurrency: int,
    images: bool = False,
) -> list[Any]:
    """Each request's response body, or None for a row sent nothing (None) or answered with nothing (with a
    warning). The model is checked first, once there is something to send."""
    sending = [request for request in requests if request is not None]
    if not sending:
        return [None] * len(requests)
    session = client.session()
    listed = session.find_model(function, model, host.poll)
    if images and not listed.reads_images:
        raise DrexInputError(f"{function}: {model} does not read images; use an image model such as drex-max")
    for request in sending:
        request.model = listed.name
    results = iter(session.run(function, sending, concurrency, host.poll))
    bodies = []
    for position, request in enumerate(requests, 1):
        if request is None:
            bodies.append(None)
            continue
        result = next(results)
        if result.error is not None:
            row = f"row {position}" if len(requests) > 1 else "a row"
            host.warning(f"{function}: {row} returned NULL: {result.error}")
        bodies.append(result.body if result.error is None else None)
    return bodies


def _typed(
    host: Host,
    function: str,
    question_type: str,
    input: str | None,
    json_input: bool,
    instructions: str | None,
    criteria: str | None,
    model: str | None,
) -> Any:
    question: Question = typed_question(function, question_type, instructions, criteria)
    if input is None:
        return None
    state = parse_json(function, "input", input) if json_input else input
    request = client.Request(state=state, questions=questions_to_drex((question,)))
    [body] = _send(host, function, _model(model, DEFAULT_MODEL), [request], 1)
    if body is None:
        return None
    value = answer_value(function, question, body)
    return value if question_type == "noul" else json.dumps(value)


def drex_noul(host: Host, input: str | None, instructions: str | None, criteria: str | None, model: str | None, json_input: bool = False) -> float | None:
    return _typed(host, "drex_noul", "noul", input, json_input, instructions, criteria, model)


def drex_choice(host: Host, input: str | None, instructions: str | None, labels: str | None, model: str | None, json_input: bool = False) -> str | None:
    return _typed(host, "drex_choice", "choice", input, json_input, instructions, labels, model)


def drex_score(host: Host, input: str | None, instructions: str | None, levels: str | None, model: str | None, json_input: bool = False) -> str | None:
    return _typed(host, "drex_score", "score", input, json_input, instructions, levels, model)


def _one_dimensional(function: str, name: str, values: Sequence[Any] | None) -> None:
    # PL/Python passes a multidimensional array as nested lists
    if values is not None and any(isinstance(value, list) for value in values):
        raise DrexInputError(f"{function}: {name} must be a one-dimensional array")


def _questions(function: str, questions: str | None) -> dict[str, Any] | None:
    return None if questions is None else questions_to_drex(named_questions(function, questions))


def drex_decide(host: Host, state: str | None, questions: str | None, model: str | None) -> str | None:
    function = "drex_decide"
    drex_questions = _questions(function, questions)
    if state is None or drex_questions is None:
        return None
    request = client.Request(state=parse_json(function, "state", state), questions=drex_questions)
    [body] = _send(host, function, _model(model, DEFAULT_MODEL), [request], 1)
    return None if body is None else json.dumps(body)


def drex_decide_image(host: Host, state: str | None, images: Sequence[bytes | None] | None, questions: str | None, model: str | None) -> str | None:
    function = "drex_decide_image"
    _one_dimensional(function, "images", images)
    drex_questions = _questions(function, questions)
    # A NULL image, or no images, sends nothing
    if state is None or drex_questions is None or not images or any(image is None for image in images):
        return None
    request = client.Request(
        state=parse_json(function, "state", state), questions=drex_questions, images=[bytes(image) for image in images if image is not None]
    )
    [body] = _send(host, function, _model(model, DEFAULT_IMAGE_MODEL), [request], 1, images=True)
    return None if body is None else json.dumps(body)


def drex_decide_batch(host: Host, states: Sequence[str | None] | None, questions: str | None, model: str | None, concurrency: int | None) -> list[str | None] | None:
    """One result per state, in order, with NULL for a NULL state or a failed row; up to `concurrency` requests in
    flight at once."""
    function = "drex_decide_batch"
    limit = DEFAULT_CONCURRENCY if concurrency is None else concurrency
    if limit < 1:
        raise DrexInputError(f"{function}: concurrency must be at least 1")
    _one_dimensional(function, "states", states)
    drex_questions = _questions(function, questions)
    if states is None or drex_questions is None:
        return None
    requests = [
        None if state is None else client.Request(state=parse_json(function, "state", state), questions=drex_questions)
        for state in states
    ]
    bodies = _send(host, function, _model(model, DEFAULT_MODEL), requests, limit)
    return [None if body is None else json.dumps(body) for body in bodies]
