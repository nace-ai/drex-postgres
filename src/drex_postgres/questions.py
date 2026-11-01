"""The checks every function makes before it sends anything, and Drex's answers as SQL results.

Ported from drex-duckdb's `src/drex_functions.cpp`, so both read criteria and questions the same way: a list of
labels, a list of `{"label", "description"}` objects, or (for a noul or a choice) Drex's own object of label to
description.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from drex_postgres.errors import DrexError, DrexInputError

#: Drex's limits: questions per request, and labels per choice or levels per score
MAX_QUESTIONS = 512
MIN_LABELS = 2
MAX_LABELS = 255

QUESTION_TYPES = ("noul", "choice", "score")
#: The SQL argument that holds each typed helper's criteria
ARGUMENTS = {"noul": "criteria", "choice": "labels", "score": "levels"}


@dataclass(frozen=True)
class Label:
    label: str
    #: None when the label has no description; a JSON null description is no description
    description: str | None = None


@dataclass(frozen=True)
class Question:
    id: str
    type: str
    instructions: str
    labels: tuple[Label, ...]
    #: Whether the labels were written as Drex's object of label to description, where a noul may describe only one
    labels_from_object: bool = False


def parse_json(function: str, name: str, text: str) -> Any:
    """A jsonb argument, which Postgres hands over as text."""
    try:
        return json.loads(text)
    except ValueError as error:
        raise DrexInputError(f"{function}: {name} is not valid JSON: {error}") from None


def _shape_error(function: str, name: str, allow_object: bool) -> DrexInputError:
    forms = 'a JSON array of labels or of {"label", "description"} objects'
    return DrexInputError(f"{function}: {name} must be {forms}" + (", or an object of label to description" if allow_object else ""))


def labels_from_json(function: str, name: str, value: Any, allow_object: bool) -> tuple[Label, ...]:
    def text_or_none(field: Any) -> str | None:
        if field is None or isinstance(field, str):
            return field
        raise _shape_error(function, name, allow_object)

    def make(label: str | None, description: str | None) -> Label:
        if label is None:
            raise DrexInputError(f"{function}: {name} labels must not be NULL")
        return Label(label, description)

    if isinstance(value, list):
        labels = []
        for element in value:
            if isinstance(element, dict):
                if not set(element) <= {"label", "description"}:
                    raise _shape_error(function, name, allow_object)
                labels.append(make(text_or_none(element.get("label")), text_or_none(element.get("description"))))
            else:
                labels.append(make(text_or_none(element), None))
        return tuple(labels)
    if isinstance(value, dict) and allow_object:
        return tuple(make(label, text_or_none(description)) for label, description in value.items())
    raise _shape_error(function, name, allow_object)


def check_labels(function: str, name: str, question: Question) -> None:
    labels = question.labels
    if question.type == "noul":
        # No labels is a noul without criteria
        found = [label.label for label in labels]
        valid = all(label in ("true", "false") for label in found) and len(set(found)) == len(found)
        complete = not labels or question.labels_from_object or set(found) == {"true", "false"}
        if not valid or not complete:
            raise DrexInputError(f"{function}: {name} labels must be exactly true and false, got [{', '.join(found)}]")
        return
    kind = "labels" if question.type == "choice" else "levels"
    if not MIN_LABELS <= len(labels) <= MAX_LABELS:
        raise DrexInputError(f"{function}: {name} takes {MIN_LABELS} to {MAX_LABELS} {kind}, got {len(labels)}")
    seen: set[str] = set()
    for label in labels:
        if not label.label:
            raise DrexInputError(f"{function}: {name} {kind} must not be empty")
        if label.label in seen:
            raise DrexInputError(f"{function}: {name} {kind} must be unique; '{label.label}' is a duplicate")
        seen.add(label.label)
        if question.type == "score" and label.description is not None:
            raise DrexInputError(f"{function}: score levels take no description; describe them in instructions")


def check_noul_has_text(function: str, name: str, question: Question) -> None:
    """What Drex would refuse in a noul: one with neither instructions nor a description of true or false."""
    if question.type != "noul" or question.instructions:
        return
    if any(label.description for label in question.labels):
        return
    raise DrexInputError(f"{function}: {name} needs instructions, or a description of true or false")


def parse_question_type(function: str, name: str, question_type: str) -> str:
    if question_type in QUESTION_TYPES:
        return question_type
    if question_type in ("multiple_choice", "box"):
        raise DrexInputError(f"{function}: {name}: {question_type} questions are not supported, since Drex has no {question_type} question")
    raise DrexInputError(f"{function}: {name}.type must be noul, choice or score, not '{question_type}'")


def questions_from_json(function: str, value: Any) -> tuple[Question, ...]:
    """The questions of a `questions` argument: an object of question id to {type, instructions, criteria}."""
    if not isinstance(value, dict):
        raise DrexInputError(f"{function}: questions must be a JSON object of question id to question")
    questions = []
    for question_id, entry in value.items():
        name = f"questions.{question_id}"
        if not isinstance(entry, dict):
            raise DrexInputError(f"{function}: {name} must be an object with type, instructions and criteria")
        for key in entry:
            if key not in ("type", "instructions", "criteria"):
                raise DrexInputError(f"{function}: {name} has an unknown key '{key}'; use type, instructions and criteria")
        question_type = entry.get("type")
        if not isinstance(question_type, str):
            raise DrexInputError(f"{function}: {name}.type must be noul, choice or score")
        question_type = parse_question_type(function, name, question_type)
        instructions = entry.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise DrexInputError(f"{function}: {name}.instructions must be a string")
        criteria = entry.get("criteria")
        labels: tuple[Label, ...] = ()
        if criteria is not None:
            labels = labels_from_json(function, f"{name}.criteria", criteria, question_type != "score")
        elif question_type != "noul":
            raise DrexInputError(f"{function}: {name} needs criteria")
        questions.append(Question(question_id, question_type, instructions or "", labels, isinstance(criteria, dict)))
    return tuple(questions)


def check_questions(function: str, questions: tuple[Question, ...], named: bool) -> None:
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise DrexInputError(f"{function}: questions takes 1 to {MAX_QUESTIONS} questions, got {len(questions)}")
    ids: set[str] = set()
    for question in questions:
        name = f"questions.{question.id}" if named else question.id
        if not question.id.strip():
            raise DrexInputError(f"{function}: question ids must not be empty")
        # Drex collects answers in a plain object, whose __proto__ is not a key, so it would bill the request and drop
        # the answer
        if question.id == "__proto__":
            raise DrexInputError(f"{function}: the question id '__proto__' is not allowed: Drex can't answer it")
        if question.id.lower() in ids:
            raise DrexInputError(f"{function}: question ids must be unique ignoring case; '{question.id}' is a duplicate")
        ids.add(question.id.lower())
        check_labels(function, name, question)
        check_noul_has_text(function, name, question)


def questions_to_drex(questions: tuple[Question, ...]) -> dict[str, dict[str, Any]]:
    """The `questions` object Drex takes, with `instructions` always a string (OpenRouter refuses it missing)."""
    result: dict[str, dict[str, Any]] = {}
    for question in questions:
        drex: dict[str, Any] = {"type": question.type, "instructions": question.instructions}
        if question.type == "score":
            drex["criteria"] = [label.label for label in question.labels]
        elif question.type == "choice":
            drex["criteria"] = {label.label: label.description for label in question.labels}
        elif any(label.description is not None for label in question.labels):
            # Labels alone are the same as no criteria
            drex["criteria"] = {label.label: label.description for label in question.labels}
        result[question.id] = drex
    return result


@lru_cache(maxsize=256)
def typed_question(function: str, question_type: str, instructions: str | None, criteria: str | None) -> Question:
    """The one question of drex_noul, drex_choice or drex_score, checked. Cached, since Postgres calls the function
    once per row with the same constants."""
    argument = ARGUMENTS[question_type]
    labels: tuple[Label, ...] = ()
    from_object = False
    if criteria is not None:
        value = parse_json(function, argument, criteria)
        labels = labels_from_json(function, argument, value, question_type != "score")
        from_object = isinstance(value, dict)
    elif question_type != "noul":
        raise DrexInputError(f"{function}: {argument} must not be NULL")
    question = Question(question_type, question_type, instructions or "", labels, from_object)
    check_questions(function, (question,), named=False)
    return question


@lru_cache(maxsize=256)
def named_questions(function: str, questions: str) -> tuple[Question, ...]:
    """The questions of drex_decide and its relatives, checked. Cached like typed_question."""
    parsed = questions_from_json(function, parse_json(function, "questions", questions))
    check_questions(function, parsed, named=True)
    return parsed


# ---------------------------------------------------------------------------------------------------------------------
# Answers, in the shared SQL surface's shapes
# ---------------------------------------------------------------------------------------------------------------------


def _number(function: str, question: Question, answer: dict[str, Any], key: str) -> float:
    value = answer.get(key) if isinstance(answer, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DrexError(f"{function}: Drex's answer to question '{question.id}' has no number {key}")
    return float(value)


def answer_value(function: str, question: Question, body: Any) -> Any:
    """Drex's answer to one question: a noul's probability, or a choice's or score's object with its probabilities as
    rows, a choice's sorted by label and a score's by index."""
    answers = body.get("answers") if isinstance(body, dict) else None
    answer = answers.get(question.id) if isinstance(answers, dict) else None
    if not isinstance(answer, dict):
        raise DrexError(f"{function}: Drex's response has no answer to question '{question.id}'")
    if question.type == "noul":
        return _number(function, question, answer, "noul")
    probabilities = answer.get("probabilities")
    confidence = _number(function, question, answer, "confidence")
    if question.type == "choice":
        choice = answer.get("choice")
        if not isinstance(choice, str):
            raise DrexError(f"{function}: Drex's answer to question '{question.id}' has no choice")
        # Sorted by UTF-8 bytes, as drex-duckdb sorts them, which code point order matches
        rows = [
            {"value": label, "probability": _number(function, question, probabilities, label)}
            for label in sorted(label.label for label in question.labels)
        ]
        return {"choice": choice, "probabilities": rows, "confidence": confidence}
    rows = [
        {"index": index, "value": label.label, "probability": _number(function, question, probabilities, str(index))}
        for index, label in enumerate(question.labels)
    ]
    return {"score": _number(function, question, answer, "score"), "probabilities": rows, "confidence": confidence}
