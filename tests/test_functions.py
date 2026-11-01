"""The functions against the mock Drex API, without Postgres: answers on both models, images, criteria forms, errors,
retries and concurrency, and the shared SQL surface's argument and error rules."""

from __future__ import annotations

import json
import time

import pytest
from conftest import BLUE, MOCK_KEY, RED, Host, Mock

from drex_postgres import __version__, client
from drex_postgres.errors import DrexAuthError, DrexCreditError, DrexError, DrexInputError
from drex_postgres.functions import (
    drex_choice,
    drex_decide,
    drex_decide_batch,
    drex_decide_image,
    drex_noul,
    drex_score,
)

TICKET = "I was charged twice for my March invoice. Please refund one of the charges."
CATEGORIES = json.dumps(
    {
        "billing": "Payments, invoices, refunds, or charges",
        "technical": "Bugs, errors, or outages",
        "account": "Login, profile, or permissions",
        "other": None,
    }
)
UPSET = json.dumps(["Calm", "Mildly annoyed", "Frustrated", "Angry"])
COLOURS = json.dumps({"colour": {"type": "choice", "instructions": "Which colour is Image 1?", "criteria": ["red", "blue"]}})


@pytest.fixture
def api(mock: Mock, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Points the client at the mock: `api("429-once")` returns the namespace, which is the test's name."""
    ns = request.node.name.replace("[", "_").replace("]", "").replace("-", "_")[:60]
    mock.reset(ns)

    def use(scenario: str = "ok") -> str:
        monkeypatch.setenv("NACE_API_KEY", MOCK_KEY)
        monkeypatch.setenv("NACE_BASE_URL", f"{mock.url}/ns/{ns}/{scenario}")
        return ns

    return use


# -- noul, choice and score on both models -----------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["drex-v2.0", "drex-max"])
def test_typed_helpers_answer_in_the_shared_shapes(api, mock: Mock, model: str) -> None:
    ns = api()
    host = Host()
    assert drex_noul(host, TICKET, "Is the customer asking for a refund?", None, model) == 0.9868
    assert json.loads(drex_choice(host, TICKET, "What is this support ticket about?", CATEGORIES, model)) == {
        "choice": "billing",
        # Sorted by label
        "probabilities": [
            {"value": "account", "probability": 0.0006},
            {"value": "billing", "probability": 0.9895},
            {"value": "other", "probability": 0.0049},
            {"value": "technical", "probability": 0.005},
        ],
        "confidence": 0.986,
    }
    assert json.loads(drex_score(host, TICKET, "How upset is the customer?", UPSET, model)) == {
        "score": 1.5306,
        "probabilities": [
            {"index": 0, "value": "Calm", "probability": 0.1572},
            {"index": 1, "value": "Mildly annoyed", "probability": 0.3516},
            {"index": 2, "value": "Frustrated", "probability": 0.2946},
            {"index": 3, "value": "Angry", "probability": 0.1966},
        ],
        "confidence": 0.7183,
    }
    requests = mock.requests(ns)
    assert [r["model"] for r in requests] == [model] * 3
    assert {r["x_nace_client"] for r in requests} == {f"drex-postgres/{__version__}"}
    assert all(r["state_type"] == "string" and r["instructions_present"] for r in requests)
    assert host.warnings == []


def test_json_input_is_sent_as_structured_state(api, mock: Mock) -> None:
    ns = api()
    drex_noul(Host(), json.dumps({"message": TICKET}), "Is the customer asking for a refund?", None, None, json_input=True)
    [request] = mock.requests(ns)
    assert request["state_type"] == "object"
    assert request["model"] == "drex-v2.0"


def test_a_mixed_batch_of_questions_goes_in_one_request(api, mock: Mock) -> None:
    ns = api()
    questions = {
        "refund": {"type": "noul", "instructions": "Is the customer asking for a refund?"},
        "team": {"type": "choice", "instructions": "Which team?", "criteria": ["billing", "tech"]},
        "urgency": {"type": "score", "instructions": "How urgent?", "criteria": ["low", "medium", "high"]},
    }
    body = json.loads(drex_decide(Host(), json.dumps(TICKET), json.dumps(questions), None))
    # The whole response body, unchanged
    assert set(body) == {"model", "answers", "usage", "evaluation_time_ms", "request_id"}
    assert body["answers"]["refund"]["noul"] == 0.9868
    assert body["answers"]["team"]["choice"] == "billing"
    assert body["answers"]["urgency"]["type"] == "score"
    [request] = mock.requests(ns)
    sent = json.loads(request["questions"])
    # Criteria are sent as Drex reads them: a choice's as an object, a score's as a list
    assert {q["id"]: q.get("criteria") for q in sent} == {
        "refund": None,
        "team": {"billing": None, "tech": None},
        "urgency": ["low", "medium", "high"],
    }


# -- images ------------------------------------------------------------------------------------------------------------


def test_drex_max_reads_one_image_then_two_then_two_reversed(api, mock: Mock) -> None:
    ns = api()
    host = Host()
    state = json.dumps("")
    which = json.dumps({"colour": {"type": "choice", "instructions": "Which colour is Image 2?", "criteria": ["red", "blue"]}})
    one = json.loads(drex_decide_image(host, state, [BLUE], COLOURS, None))
    two = json.loads(drex_decide_image(host, state, [RED, BLUE], COLOURS, None))
    reversed_ = json.loads(drex_decide_image(host, state, [BLUE, RED], COLOURS, None))
    second = json.loads(drex_decide_image(host, state, [RED, BLUE], which, None))
    assert [one["answers"]["colour"]["choice"], two["answers"]["colour"]["choice"]] == ["blue", "red"]
    assert [reversed_["answers"]["colour"]["choice"], second["answers"]["colour"]["choice"]] == ["blue", "blue"]
    requests = mock.requests(ns)
    # drex_decide_image with no model uses drex-max
    assert [r["model"] for r in requests] == ["drex-max"] * 4
    assert [r["image_colours"] for r in requests] == [["blue"], ["red", "blue"], ["blue", "red"], ["red", "blue"]]


@pytest.mark.parametrize("model", ["drex-v2.0", "drex-latest"])
def test_images_with_a_model_that_does_not_read_them_fail_the_query(api, mock: Mock, model: str) -> None:
    ns = api()
    with pytest.raises(DrexInputError, match=f"{model} does not read images; use an image model such as drex-max"):
        drex_decide_image(Host(), json.dumps(""), [RED], COLOURS, model)
    assert mock.requests(ns) == []


def test_an_alias_of_an_image_model_reads_images(api, mock: Mock) -> None:
    ns = api()
    # drex-v1.0 is retired, served by drex-max
    drex_decide_image(Host(), json.dumps(""), [RED], COLOURS, "drex-v1.0")
    assert [r["model"] for r in mock.requests(ns)] == ["drex-v1.0"]


def test_a_null_image_or_no_images_sends_nothing(api, mock: Mock) -> None:
    ns = api()
    host = Host()
    assert drex_decide_image(host, json.dumps(""), [RED, None], COLOURS, None) is None
    assert drex_decide_image(host, json.dumps(""), [], COLOURS, None) is None
    assert drex_decide_image(host, json.dumps(""), None, COLOURS, None) is None
    assert mock.log(ns) == [] and host.warnings == []


def test_an_image_that_is_not_png_jpeg_or_webp_is_a_null_row(api, mock: Mock) -> None:
    ns = api()
    host = Host()
    assert drex_decide_image(host, json.dumps(""), [RED, b"GIF89a..."], COLOURS, None) is None
    assert host.warnings == ["drex_decide_image: a row returned NULL: image 2 is not a PNG, JPEG or WebP image"]
    assert mock.requests(ns) == []


# -- criteria forms ----------------------------------------------------------------------------------------------------


def test_noul_with_criteria_and_no_instructions(api, mock: Mock) -> None:
    ns = api()
    criteria = json.dumps({"true": "Many users cannot use the product", "false": "A single user or a question"})
    assert drex_noul(Host(), "The site is down for everyone", None, criteria, None) == 0.75
    [request] = mock.requests(ns)
    [question] = json.loads(request["questions"])
    assert question["instructions"] == "" and question["criteria"] == json.loads(criteria)


def test_noul_labels_alone_are_no_criteria(api, mock: Mock) -> None:
    ns = api()
    drex_noul(Host(), TICKET, "Refund?", json.dumps(["true", "false"]), None)
    [question] = json.loads(mock.requests(ns)[0]["questions"])
    assert "criteria" not in question


@pytest.mark.parametrize(
    "labels",
    [
        {"billing": "Money", "other": None},
        [{"label": "billing", "description": "Money"}, {"label": "other", "description": None}],
        [{"label": "billing", "description": "Money"}, {"label": "other"}],
    ],
)
def test_a_choice_option_with_a_null_description(api, mock: Mock, labels) -> None:
    ns = api()
    result = json.loads(drex_choice(Host(), TICKET, "Which team?", json.dumps(labels), None))
    assert [row["value"] for row in result["probabilities"]] == ["billing", "other"]
    [question] = json.loads(mock.requests(ns)[0]["questions"])
    assert question["criteria"] == {"billing": "Money", "other": None}


@pytest.mark.parametrize(
    ("function", "argument", "message"),
    [
        (drex_choice, ["only"], "choice takes 2 to 255 labels, got 1"),
        (drex_choice, [str(i) for i in range(256)], "choice takes 2 to 255 labels, got 256"),
        (drex_choice, ["a", "a"], "choice labels must be unique; 'a' is a duplicate"),
        (drex_choice, ["a", ""], "choice labels must not be empty"),
        (drex_choice, ["a", None], "labels labels must not be NULL"),
        (drex_choice, [1, 2], "labels must be a JSON array of labels"),
        (drex_choice, [{"label": "a", "colour": "red"}, "b"], "labels must be a JSON array of labels"),
        (drex_score, [{"label": "low", "description": "x"}, "high"], "score levels take no description"),
        (drex_score, {"low": None, "high": None}, r"levels must be a JSON array of labels or of \{\"label\", \"description\"\} objects$"),
        (drex_noul, ["yes", "no"], r"noul labels must be exactly true and false, got \[yes, no\]"),
        (drex_noul, ["true"], r"noul labels must be exactly true and false, got \[true\]"),
    ],
)
def test_label_rules_fail_the_query_before_any_request(api, mock: Mock, function, argument, message) -> None:
    ns = api()
    with pytest.raises(DrexInputError, match=message):
        function(Host(), TICKET, "Question?", json.dumps(argument), None)
    assert mock.log(ns) == []


def test_choice_needs_labels(api) -> None:
    api()
    with pytest.raises(DrexInputError, match="drex_choice: labels must not be NULL"):
        drex_choice(Host(), TICKET, "Which?", None, None)


def test_a_noul_needs_instructions_or_a_described_outcome(api, mock: Mock) -> None:
    ns = api()
    with pytest.raises(DrexInputError, match="noul needs instructions, or a description of true or false"):
        drex_noul(Host(), TICKET, "", None, None)
    assert mock.log(ns) == []


@pytest.mark.parametrize(
    ("questions", "message"),
    [
        ({}, "questions takes 1 to 512 questions, got 0"),
        ({str(i): {"type": "noul", "instructions": "x"} for i in range(513)}, "questions takes 1 to 512 questions, got 513"),
        ({"__proto__": {"type": "noul", "instructions": "x"}}, "the question id '__proto__' is not allowed"),
        ({"A": {"type": "noul", "instructions": "x"}, "a": {"type": "noul", "instructions": "y"}}, "unique ignoring case; 'a'"),
        ({" ": {"type": "noul", "instructions": "x"}}, "question ids must not be empty"),
        ({"a": {"type": "box", "instructions": "x"}}, "questions.a: box questions are not supported"),
        ({"a": {"type": "multiple_choice", "instructions": "x"}}, "multiple_choice questions are not supported"),
        ({"a": {"type": "noul", "instructions": "x", "weight": 2}}, "questions.a has an unknown key 'weight'"),
        ({"a": {"type": "choice", "instructions": "x"}}, "questions.a needs criteria"),
        ({"a": {"type": "noul", "instructions": 5}}, r"questions.a.instructions must be a string"),
        (["a"], "questions must be a JSON object of question id to question"),
    ],
)
def test_question_rules_fail_the_query_in_drex_decide(api, mock: Mock, questions, message) -> None:
    ns = api()
    with pytest.raises(DrexInputError, match=message):
        drex_decide(Host(), json.dumps(TICKET), json.dumps(questions), None)
    assert mock.log(ns) == []


def test_512_questions_are_allowed(api, mock: Mock) -> None:
    ns = api()
    questions = {f"q{i}": {"type": "noul", "instructions": f"Question {i}?"} for i in range(512)}
    body = json.loads(drex_decide(Host(), json.dumps(TICKET), json.dumps(questions), None))
    assert len(body["answers"]) == 512
    assert mock.requests(ns)[0]["question_count"] == 512


# -- NULLs, models and the key -----------------------------------------------------------------------------------------


def test_null_inputs_return_null_and_send_nothing(api, mock: Mock) -> None:
    ns = api()
    host = Host()
    assert drex_noul(host, None, "Refund?", None, None) is None
    assert drex_choice(host, None, "Team?", json.dumps(["a", "b"]), None) is None
    assert drex_decide(host, None, json.dumps({"a": {"type": "noul", "instructions": "x"}}), None) is None
    assert drex_decide(host, json.dumps("x"), None, None) is None
    assert drex_decide_batch(host, None, json.dumps({"a": {"type": "noul", "instructions": "x"}}), None, None) is None
    assert mock.log(ns) == [] and host.warnings == []


def test_a_null_input_still_checks_the_arguments(api) -> None:
    api()
    with pytest.raises(DrexInputError, match="choice takes 2 to 255 labels"):
        drex_choice(Host(), None, "Team?", json.dumps(["a"]), None)


def test_an_unknown_model_fails_the_query(api, mock: Mock) -> None:
    ns = api()
    with pytest.raises(DrexInputError, match="unknown model 'drex-v9'; GET /v1/models lists drex-v1.5, drex-v2.0"):
        drex_noul(Host(), TICKET, "Refund?", None, "drex-v9")
    assert [r["endpoint"] for r in mock.log(ns)] == ["/v1/models"]


def test_the_model_list_is_cached_per_process(api, mock: Mock) -> None:
    ns = api()
    for _ in range(3):
        drex_noul(Host(), TICKET, "Refund?", None, "drex-max")
    assert [r["endpoint"] for r in mock.log(ns)].count("/v1/models") == 1


def test_null_or_empty_model_means_the_default(api, mock: Mock) -> None:
    ns = api()
    drex_noul(Host(), TICKET, "Refund?", None, None)
    drex_noul(Host(), TICKET, "Refund?", None, "")
    drex_decide_image(Host(), json.dumps(""), [RED], COLOURS, "")
    assert [r["model"] for r in mock.requests(ns)] == ["drex-v2.0", "drex-v2.0", "drex-max"]


def test_no_key_fails_the_query(api, monkeypatch: pytest.MonkeyPatch) -> None:
    api()
    monkeypatch.delenv("NACE_API_KEY")
    with pytest.raises(DrexAuthError, match="NACE_API_KEY is not set in the Postgres server's environment"):
        drex_noul(Host(), TICKET, "Refund?", None, None)


@pytest.mark.parametrize("base_url", ["http://drex.example.com", "ftp://localhost", "console.nace.ai"])
def test_a_base_url_that_would_send_the_key_in_cleartext_is_refused(api, monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    api()
    monkeypatch.setenv("NACE_BASE_URL", base_url)
    with pytest.raises(DrexAuthError, match="NACE_BASE_URL must be an https:// URL"):
        drex_noul(Host(), TICKET, "Refund?", None, None)


# -- errors ------------------------------------------------------------------------------------------------------------


def test_401_fails_the_query(api) -> None:
    api("401")
    with pytest.raises(DrexAuthError, match=r"Invalid API key.*\(request req_"):
        drex_noul(Host(), TICKET, "Refund?", None, None)


def test_402_fails_the_query_with_the_billing_page(api, mock: Mock) -> None:
    ns = api("402")
    with pytest.raises(DrexCreditError, match=f"insufficient available funds.*Billing: {mock.url}/ns/{ns}/402/dashboard/billing"):
        drex_noul(Host(), TICKET, "Refund?", None, None)


def test_a_422_about_the_query_fails_it_with_every_issue(api) -> None:
    api("422")
    with pytest.raises(
        DrexInputError,
        match=r"Drex refused the request: questions.a.instructions: must be a string; "
        r"questions.a.instructions: noul needs instructions or criteria \(request req_",
    ):
        drex_noul(Host(), TICKET, "Refund?", None, None)


def test_a_row_over_the_token_limit_is_null_and_the_others_answer(api, mock: Mock) -> None:
    api("too-long")
    host = Host()
    states = [json.dumps("short"), json.dumps("x" * 10_000), json.dumps("also short")]
    results = drex_decide_batch(host, states, json.dumps({"a": {"type": "noul", "instructions": "x"}}), None, None)
    assert [result is None for result in results] == [False, True, False]
    [warning] = host.warnings
    assert warning.startswith("drex_decide_batch: row 2 returned NULL: HTTP 422: state: exceeds the 1,024-token state limit")
    # The warning never quotes the row
    assert "xxx" not in warning


def test_an_oversized_row_is_null_without_a_request_and_the_query_continues(api, mock: Mock) -> None:
    ns = api()
    host = Host()
    states = [json.dumps("a"), json.dumps("x" * (4 * 1024 * 1024)), json.dumps("b")]
    results = drex_decide_batch(host, states, json.dumps({"a": {"type": "noul", "instructions": "x"}}), None, None)
    assert [result is None for result in results] == [False, True, False]
    assert host.warnings[0].startswith("drex_decide_batch: row 2 returned NULL: the request body is ")
    # Only a row with images is told to resize them
    assert host.warnings[0].endswith(f"over Drex's limit of {4 * 1024 * 1024}")
    assert len(mock.requests(ns)) == 2


def test_a_multidimensional_array_fails_the_query(api, mock: Mock) -> None:
    ns = api()
    # PL/Python passes a multidimensional array as nested lists
    with pytest.raises(DrexInputError, match="drex_decide_batch: states must be a one-dimensional array"):
        drex_decide_batch(Host(), [[json.dumps("a")], [json.dumps("b")]], json.dumps({"a": {"type": "noul", "instructions": "x"}}), None, None)
    with pytest.raises(DrexInputError, match="drex_decide_image: images must be a one-dimensional array"):
        drex_decide_image(Host(), json.dumps(""), [[RED]], COLOURS, None)
    assert mock.log(ns) == []


@pytest.mark.parametrize(("scenario", "wait_ms"), [("429-once", 1000), ("529-once", 2000)])
def test_429_and_529_are_retried_after_retry_after_ms(api, mock: Mock, scenario: str, wait_ms: int) -> None:
    ns = api(scenario)
    assert drex_noul(Host(), TICKET, "Is the customer asking for a refund?", None, None) == 0.9868
    first, second = mock.requests(ns)
    assert (first["status"], second["status"]) == (int(scenario[:3]), 200)
    assert second["waited_ms"] >= wait_ms
    # Every attempt sends the same body and key
    assert first["body_sha256"] == second["body_sha256"] and first["idempotency_key"] == second["idempotency_key"]


def test_retries_running_out_make_the_row_null(api, mock: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    ns = api("529-always")
    # The mock asks for 2 s each time; a shorter budget keeps the test short and shows the budget stops retries
    monkeypatch.setattr(client, "REQUEST_BUDGET_SECONDS", 5.0)
    host = Host()
    assert drex_noul(host, TICKET, "Refund?", None, None) is None
    assert len(mock.requests(ns)) == 3
    [warning] = host.warnings
    assert warning.startswith("drex_noul: a row returned NULL: no answer after 3 attempts, the last HTTP 529: Drex is at capacity")


def test_at_most_five_attempts(api, mock: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    ns = api("529-always")
    monkeypatch.setattr(client, "MAX_RETRY_WAIT_SECONDS", 0.01)
    assert drex_noul(Host(), TICKET, "Refund?", None, None) is None
    assert len(mock.requests(ns)) == 5


def test_a_tls_failure_fails_the_query_at_once(api, mock: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    ns = api()
    # The mock speaks plain HTTP, so a TLS handshake with it fails
    monkeypatch.setenv("NACE_BASE_URL", mock.url.replace("http://", "https://") + f"/ns/{ns}/ok")
    started = time.monotonic()
    with pytest.raises(DrexError, match="could not reach Drex over TLS"):
        drex_noul(Host(), TICKET, "Refund?", None, None)
    assert time.monotonic() - started < 5


# -- concurrency -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("concurrency", [1, 3, 8])
def test_the_concurrency_cap_holds(api, mock: Mock, concurrency: int) -> None:
    ns = api("slow-150")
    states = [json.dumps(f"ticket {i}") for i in range(24)]
    started = time.monotonic()
    results = drex_decide_batch(Host(), states, json.dumps({"a": {"type": "noul", "instructions": "x"}}), None, concurrency)
    elapsed = time.monotonic() - started
    assert len(results) == 24 and all(result is not None for result in results)
    peak = max(r["in_flight"] for r in mock.requests(ns))
    assert peak == concurrency
    # And it is used: 24 rows at 150 ms each take about 24 / concurrency * 150 ms
    assert elapsed < 24 / concurrency * 0.15 + 2


def test_batch_results_keep_the_order_of_the_states(api) -> None:
    api()
    states = [json.dumps(f"ticket {i}") for i in range(10)]
    states[4] = None
    results = drex_decide_batch(Host(), states, json.dumps({"a": {"type": "noul", "instructions": "x"}}), None, 4)
    assert results[4] is None and all(result is not None for i, result in enumerate(results) if i != 4)


def test_concurrency_below_one_fails_the_query(api) -> None:
    api()
    with pytest.raises(DrexInputError, match="concurrency must be at least 1"):
        drex_decide_batch(Host(), [json.dumps("x")], json.dumps({"a": {"type": "noul", "instructions": "x"}}), None, 0)


def test_a_waiting_call_polls_for_a_cancel(api) -> None:
    api("slow-700")
    host = Host()
    drex_noul(host, TICKET, "Refund?", None, None)
    assert host.polls >= 2


def test_a_cancel_stops_the_call(api, mock: Mock) -> None:
    ns = api("slow-500")

    class Cancelled(Exception):
        pass

    class CancellingHost(Host):
        def poll(self) -> None:
            raise Cancelled

    states = [json.dumps(f"ticket {i}") for i in range(20)]
    started = time.monotonic()
    with pytest.raises(Cancelled):
        drex_decide_batch(CancellingHost(), states, json.dumps({"a": {"type": "noul", "instructions": "x"}}), None, 2)
    assert time.monotonic() - started < 1
    time.sleep(0.6)
    # Only the requests in flight when it was cancelled were sent
    assert len(mock.requests(ns)) == 2


# -- OpenRouter --------------------------------------------------------------------------------------------------------


@pytest.fixture
def open_router(mock: Mock, monkeypatch: pytest.MonkeyPatch, tmp_path, request: pytest.FixtureRequest) -> str:
    """Sends https://openrouter.ai/api through the mock, which answers as OpenRouter does, with its own certificate."""
    import urllib.request

    certificate = tmp_path / "openrouter.pem"
    certificate.write_bytes(urllib.request.urlopen(f"{mock.url}/__fixtures/openrouter.pem").read())
    monkeypatch.setenv("NACE_API_KEY", "sk-or-v1-" + "o" * 40)
    monkeypatch.setenv("NACE_BASE_URL", "https://openrouter.ai/api")
    monkeypatch.setenv("HTTPS_PROXY", mock.url)
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
    monkeypatch.setattr(client, "_sessions", {})
    mock.reset("default")
    return "default"


def test_open_router_takes_its_own_body_and_headers(open_router: str, mock: Mock) -> None:
    host = Host()
    assert drex_noul(host, TICKET, "Is the customer asking for a refund?", None, None) == 0.9868
    one = json.loads(drex_decide_image(host, json.dumps("look"), [RED, BLUE], COLOURS, None))
    assert one["answers"]["colour"]["choice"] == "red" and one["model"] == "nace-ai/drex-max"
    models, text, image = mock.log(open_router)
    assert models["endpoint"] == "/v1/models" and models["open_router"]
    assert [text["model"], image["model"]] == ["nace-ai/drex-v2.0", "nace-ai/drex-max"]
    # Images as state content parts after the state as text, never top-level images
    assert image["state_type"] == "array" and image["image_colours"] == ["red", "blue"]
    assert '"images"' not in image["body"]
    assert {(r["http_referer"], r["x_openrouter_title"]) for r in (text, image)} == {("https://nace.ai", "Drex for PostgreSQL")}


def test_open_router_checks_names_under_nace_ai(open_router: str) -> None:
    # A name without an owner resolves under nace-ai/, and OpenRouter doesn't serve drex-max-v1.1
    with pytest.raises(DrexInputError, match="unknown model 'drex-max-v1.1'; GET /v1/models lists nace-ai/drex-v1.5"):
        drex_noul(Host(), TICKET, "Refund?", None, "drex-max-v1.1")


def test_open_router_attribution_is_set_by_the_caller(open_router: str, mock: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NACE_OPENROUTER_REFERER", "https://app.example")
    monkeypatch.setenv("NACE_OPENROUTER_TITLE", "Example")
    assert drex_noul(Host(), TICKET, "Is the customer asking for a refund?", None, None) == 0.9868
    sent = mock.log(open_router)
    assert [r["endpoint"] for r in sent] == ["/v1/models", "/v1/systemone"]
    assert {(r["http_referer"], r["x_openrouter_title"]) for r in sent} == {("https://app.example", "Example")}


def test_a_nace_host_gets_no_attribution_headers(api, mock: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    ns = api()
    monkeypatch.setenv("NACE_OPENROUTER_REFERER", "https://app.example")
    monkeypatch.setenv("NACE_OPENROUTER_TITLE", "Example")
    assert drex_noul(Host(), TICKET, "Is the customer asking for a refund?", None, None) == 0.9868
    assert {(r["http_referer"], r["x_openrouter_title"]) for r in mock.log(ns)} == {(None, None)}


def test_a_nace_key_is_never_sent_to_open_router(open_router: str, mock: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NACE_API_KEY", MOCK_KEY)
    with pytest.raises(DrexAuthError, match="OPENROUTER_API_KEY"):
        drex_noul(Host(), TICKET, "Refund?", None, None)
    assert mock.log(open_router) == []


def test_open_router_prefers_its_own_key(open_router: str, mock: Mock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NACE_API_KEY", MOCK_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "p" * 40)
    assert drex_noul(Host(), TICKET, "Is the customer asking for a refund?", None, None) == 0.9868
    assert {r["authorization_header"] for r in mock.log(open_router)} == {"Bearer sk-or-v1-" + "p" * 40}


def test_an_open_router_key_in_nace_api_key_still_works(open_router: str, mock: Mock) -> None:
    assert drex_noul(Host(), TICKET, "Is the customer asking for a refund?", None, None) == 0.9868
    assert {r["authorization_header"] for r in mock.log(open_router)} == {"Bearer sk-or-v1-" + "o" * 40}
