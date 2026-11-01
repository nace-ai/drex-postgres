"""Calls to Drex through nace-sdk: model checks, request bodies, retries, and which failures fail the query.

The rules are drex-duckdb's (`src/drex_client.cpp`), so both behave the same:

- 401, 402, a 422 about the query rather than the row, a TLS failure and an unknown model fail the query.
- A 422 about the row (`Request too long:`, or issues only under `state` or `images`), a body over 4 MiB, an image
  that isn't a PNG, JPEG or WebP, retries running out, and any other status make the row NULL with a warning.
- No response, 408, 429, 500, 502, 503, 504 and 529 are retried: at most 5 attempts within 120 s of the first,
  waiting for `retry-after-ms` (or `retry-after`) up to 30 s, else 1, 2, 4 and 8 s less up to 25 % jitter.

The SDK's own retries are off (`RetryPolicy(max_retries=0)`): its policy can't cap a server's wait at 30 s, would
retry a TLS failure, and would hold a concurrency slot while it waits. `run` schedules the attempts instead.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
import json
import os
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from nace_sdk import NaceAPIConnectionError, NaceAPIError, NaceClient, NaceError, RetryPolicy
from pydantic import BaseModel, ConfigDict

from drex_postgres import __version__
from drex_postgres.errors import DrexAuthError, DrexCreditError, DrexError, DrexInputError

DEFAULT_BASE_URL = "https://console.nace.ai"
REQUEST_TIMEOUT_SECONDS = 60.0
#: Attempts per request, all with the same body and Idempotency-Key
MAX_ATTEMPTS = 5
MAX_RETRY_WAIT_SECONDS = 30.0
#: From a request's first attempt; a retry that would start after it is not made
REQUEST_BUDGET_SECONDS = 120.0
#: Drex refuses a larger body with a 413
MAX_BODY_BYTES = 4 * 1024 * 1024
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
#: How often a call waiting on Drex lets Postgres check for a cancel or a statement_timeout
POLL_SECONDS = 0.2

#: The versions that read images. Drex's GET /v1/models has no image field, so the list lives here; a listed model
#: reads images when it is one of these or its alias_for names one. OpenRouter lists each model's modalities
IMAGE_MODELS = frozenset({"drex-max", "drex-max-v1.1"})

CLIENT_NAME = f"drex-postgres/{__version__}"
OPENROUTER_REFERER = "https://nace.ai"
OPENROUTER_TITLE = "Drex for PostgreSQL"
OPENROUTER_BILLING_URL = "https://openrouter.ai/settings/credits"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class _Body(BaseModel):
    """Drex's response body as it came, for drex_decide to return unchanged."""

    model_config = ConfigDict(extra="allow")


def is_open_router(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def image_type(image: bytes) -> str | None:
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return None


# ---------------------------------------------------------------------------------------------------------------------
# Outcomes of one attempt
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class _Failure:
    """An attempt that got no 2xx: its status (0 when no response arrived), body and headers."""

    status: int
    body: Any = None
    headers: httpx.Headers = field(default_factory=httpx.Headers)
    transport_error: str = ""

    @property
    def error(self) -> dict[str, Any]:
        error = self.body.get("error") if isinstance(self.body, dict) else None
        return error if isinstance(error, dict) else {}

    @property
    def message(self) -> str:
        if isinstance(self.body, dict) and isinstance(self.body.get("error"), str):
            return self.body["error"]
        message = self.error.get("message")
        return message if isinstance(message, str) else ""

    @property
    def issues(self) -> list[tuple[str, str]]:
        issues = self.error.get("issues")
        if not isinstance(issues, list):
            return []
        return [
            (str(issue.get("path") or ""), str(issue.get("message") or "")) for issue in issues if isinstance(issue, dict)
        ]

    @property
    def request_id(self) -> str:
        request_id = self.body.get("request_id") if isinstance(self.body, dict) else None
        return request_id if isinstance(request_id, str) else self.headers.get("x-request-id", "")

    @property
    def retryable(self) -> bool:
        return self.status == 0 or self.status in RETRYABLE_STATUSES

    @property
    def tls_failure(self) -> bool:
        """No response arrived because TLS failed, in the handshake or on the certificate, which no retry fixes."""
        error = self.transport_error.lower()
        return self.status == 0 and ("ssl" in error or "certificate" in error)

    def wait_seconds(self, retry: int) -> float:
        """The wait before retry number `retry` (1-based)."""
        for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
            try:
                value = float(self.headers.get(name, "").strip())
            except ValueError:
                continue
            if value >= 0:
                return min(value * scale, MAX_RETRY_WAIT_SECONDS)
        backoff = float(1 << min(retry - 1, 4))
        return min(backoff - random.uniform(0, backoff / 4), MAX_RETRY_WAIT_SECONDS)


def _issue_text(path: str, message: str) -> str:
    return f"{path}: {message}" if path else message


def _is_under(path: str, field_name: str) -> bool:
    return path == field_name or path.startswith(field_name + ".")


def _refuses_row(failure: _Failure) -> bool:
    """Whether a 422 is about the row rather than the query's constants (questions, model): a request over the token
    limit, or issues only under `state` and `images`."""
    if failure.message.startswith("Request too long:"):
        return True
    issues = failure.issues
    return bool(issues) and all(_is_under(path, "state") or _is_under(path, "images") for path, _ in issues)


def _describe(failure: _Failure) -> str:
    """The response's own message, else its status. A 422 here refused only the row, and its message may quote the
    row, so only its issues are kept, with a path under `state` (the row's own keys) cut to `state`."""
    if failure.status == 0:
        return failure.transport_error or "no response"
    description = f"HTTP {failure.status}"
    if failure.status == 422:
        reason = "; ".join(_issue_text("state" if _is_under(p, "state") else p, m) for p, m in failure.issues)
    else:
        reason = failure.message
    if reason:
        description += f": {reason}"
    if failure.request_id:
        description += f" (request {failure.request_id})"
    return description


# ---------------------------------------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Request:
    """One row's request to POST /v1/systemone, or the GET of /v1/models."""

    model: str = ""
    state: Any = None
    questions: dict[str, Any] | None = None
    images: Sequence[bytes] = ()
    get_path: str | None = None


@dataclass
class Result:
    """A row's answer, or why it has none."""

    body: Any = None
    error: str | None = None


@dataclass
class _Call:
    """A request as it is sent, the same on every attempt."""

    request: Request
    state: Any = None
    images: list[str] | None = None
    idempotency_key: str = ""
    attempts: int = 0
    started: float = 0.0


@dataclass
class _Outcome:
    body: Any = None
    failure: _Failure | None = None
    fatal: BaseException | None = None


@dataclass
class ListedModel:
    #: The name a request sends
    name: str
    reads_images: bool


class Session:
    """The client for one API key and base URL, kept for the life of the Postgres backend so its connections are
    reused between rows and calls."""

    def __init__(self, api_key: str, base_url: str, referer: str = "", title: str = "") -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.open_router = is_open_router(base_url)
        headers = (
            {"HTTP-Referer": referer or OPENROUTER_REFERER, "X-OpenRouter-Title": title or OPENROUTER_TITLE}
            if self.open_router
            else {}
        )
        # Shared by every worker thread of a call (httpx.Client is thread safe), so up to `concurrency` connections
        self.http = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, limits=httpx.Limits(max_keepalive_connections=64))
        self.client = NaceClient(
            api_key=api_key,
            base_url=base_url,
            retry=RetryPolicy(max_retries=0),
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers=headers,
            http_client=self.http,
            client_info=CLIENT_NAME,
        )
        self.models: list[ListedModel] | None = None
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": CLIENT_NAME,
            "X-Nace-Client": CLIENT_NAME,
            **headers,
        }

    # -- models -------------------------------------------------------------------------------------------------------

    def find_model(self, function: str, model: str, poll: Callable[[], None]) -> ListedModel:
        """The listed model `model` is sent as, from GET /v1/models, cached and fetched again for a model the cache
        lacks, so a model released since is found. Drex answers some unregistered drex-* names, so only this check
        refuses one."""
        listed = self._find_listed(model)
        if listed is None:
            self.models = self._fetch_models(function, poll)
            listed = self._find_listed(model)
        if listed is None:
            names = ", ".join(entry.name for entry in self.models or [])
            raise DrexInputError(f"{function}: unknown model '{model}'; GET /v1/models lists {names}")
        return listed

    def _find_listed(self, model: str) -> ListedModel | None:
        # On OpenRouter, a name without an owner is under nace-ai/, as requests send it
        name = f"nace-ai/{model}" if self.open_router and "/" not in model else model
        return next((entry for entry in self.models or [] if entry.name == name), None)

    def _fetch_models(self, function: str, poll: Callable[[], None]) -> list[ListedModel]:
        # OpenRouter lists only text models unless asked for others
        path = "/v1/models?output_modalities=decisions" if self.open_router else "/v1/models"
        [result] = self.run(function, [Request(get_path=path)], 1, poll)
        if result.error is not None:
            raise DrexError(f"{function}: could not list Drex's models: {result.error}")
        body = result.body
        malformed = DrexError(f"{function}: GET /v1/models answered with an unexpected body")
        models: list[ListedModel] = []
        if isinstance(body, dict) and isinstance(body.get("models"), list):
            for entry in body["models"]:
                name = entry.get("name") if isinstance(entry, dict) else None
                if not isinstance(name, str):
                    raise malformed
                alias_for = entry.get("alias_for")
                models.append(ListedModel(name, (alias_for if isinstance(alias_for, str) else name) in IMAGE_MODELS))
        elif isinstance(body, dict) and isinstance(body.get("data"), list):
            for entry in body["data"]:
                model_id = entry.get("id") if isinstance(entry, dict) else None
                if not isinstance(model_id, str):
                    raise malformed
                if not model_id.startswith("nace-ai/"):
                    continue
                architecture = entry.get("architecture")
                modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
                models.append(ListedModel(model_id, isinstance(modalities, list) and "image" in modalities))
        else:
            raise malformed
        return models

    # -- requests -----------------------------------------------------------------------------------------------------

    def _prepare(self, request: Request) -> tuple[_Call, str | None]:
        """The call a request is sent as, or why the row is sent nothing: an image that isn't a PNG, JPEG or WebP,
        or a body over Drex's limit."""
        call = _Call(request)
        if request.get_path is not None:
            return call, None
        for index, image in enumerate(request.images, 1):
            if image_type(image) is None:
                return call, f"image {index} is not a PNG, JPEG or WebP image"
        encoded = [base64.b64encode(image).decode("ascii") for image in request.images]
        if self.open_router and encoded:
            # OpenRouter takes images as data URLs in state content parts after the state as text
            text = request.state if isinstance(request.state, str) else json.dumps(request.state)
            call.state = [{"type": "text", "text": text}] + [
                {"type": "image_url", "image_url": {"url": f"data:{image_type(image)};base64,{data}"}}
                for image, data in zip(request.images, encoded)
            ]
        else:
            call.state = request.state
            call.images = encoded or None
        body = {"state": call.state, "model": request.model, "questions": request.questions}
        if call.images:
            body["images"] = call.images
        serialized = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
        if len(serialized) > MAX_BODY_BYTES:
            advice = "; resize the images first" if request.images else ""
            return call, f"the request body is {len(serialized)} bytes, over Drex's limit of {MAX_BODY_BYTES}{advice}"
        # The same for every attempt. Drex's /v1/systemone ignores it and bills only a 200, so a retry is billed twice
        # only when a 200 was lost on its way back
        call.idempotency_key = hashlib.sha256(request.model.encode() + b"\0" + serialized).hexdigest()
        return call, None

    def _attempt(self, function: str, call: _Call) -> _Outcome:
        """One attempt, in a worker thread: its answer, its failure, or the error that fails the query."""
        try:
            request = call.request
            if request.get_path is not None:
                try:
                    response = self.http.get(self.base_url + request.get_path, headers=self._headers)
                except httpx.RequestError as error:
                    return _Outcome(failure=_Failure(0, transport_error=f"{type(error).__name__}: {error}"))
                if response.is_success:
                    return _Outcome(body=response.json())
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                return _Outcome(failure=_Failure(response.status_code, body, response.headers))
            answer = self.client.system_one(
                call.state,
                request.questions or {},
                model=request.model,
                images=call.images,
                extra_headers={"Idempotency-Key": call.idempotency_key},
                response_model=_Body,
            )
            return _Outcome(body=answer.model_dump())
        except NaceAPIConnectionError as error:
            cause = error.__cause__
            return _Outcome(failure=_Failure(0, transport_error=f"{error}: {cause}" if cause else str(error)))
        except NaceAPIError as error:
            if 200 <= error.status < 300:
                return _Outcome(fatal=DrexError(f"{function}: Drex answered with an unexpected body ({error})"))
            return _Outcome(failure=_Failure(error.status, error.body, error.headers))
        except ValueError:
            return _Outcome(fatal=DrexError(f"{function}: Drex answered with a body that is not JSON"))
        except NaceError as error:
            return _Outcome(fatal=DrexError(f"{function}: {error}"))

    def _fatal(self, function: str, failure: _Failure) -> DrexError | None:
        """The error a failure fails the query with: 401, 402, a 422 not about the row alone, and a TLS failure."""
        if failure.tls_failure:
            return DrexError(f"{function}: could not reach Drex over TLS: {failure.transport_error}")
        if failure.status not in (401, 402, 422) or (failure.status == 422 and _refuses_row(failure)):
            return None
        request = f" (request {failure.request_id})" if failure.request_id else ""
        if failure.status == 401:
            return DrexAuthError(f"{function}: {failure.message or 'the API key was refused (HTTP 401)'}{request}")
        if failure.status == 402:
            billing = OPENROUTER_BILLING_URL if self.open_router else f"{self.base_url}/dashboard/billing"
            message = failure.message or "the account has no funds left (HTTP 402)."
            return DrexCreditError(f"{function}: {message} Billing: {billing}{request}")
        issues = "; ".join(_issue_text(path, message) for path, message in failure.issues) or failure.message
        return DrexInputError(f"{function}: Drex refused the request: {issues or 'HTTP 422'}{request}")

    def run(self, function: str, requests: Sequence[Request], concurrency: int, poll: Callable[[], None]) -> list[Result]:
        """Sends every request, at most `concurrency` at a time, and returns each one's result in order.

        This thread schedules: it hands single attempts to worker threads, keeps a request that waits to retry in a
        queue (so it holds no slot), and calls `poll` while it waits, which raises once Postgres cancels the statement.
        A fatal answer or a cancel stops the call at once; attempts still in flight are left to finish on their own.
        """
        results = [Result() for _ in requests]
        calls: list[_Call] = []
        ready: deque[int] = deque()
        for index, request in enumerate(requests):
            call, error = self._prepare(request)
            calls.append(call)
            if error is None:
                ready.append(index)
            else:
                results[index].error = error
        if not ready:
            return results

        retries: list[tuple[float, int]] = []
        in_flight: dict[Future[_Outcome], int] = {}
        pool = ThreadPoolExecutor(max_workers=min(concurrency, len(ready)), thread_name_prefix="drex")
        finished = False
        last_poll = time.monotonic()
        try:
            while ready or retries or in_flight:
                now = time.monotonic()
                while len(in_flight) < concurrency:
                    if retries and retries[0][0] <= now:
                        index = heapq.heappop(retries)[1]
                    elif ready:
                        index = ready.popleft()
                        calls[index].started = now
                    else:
                        break
                    calls[index].attempts += 1
                    in_flight[pool.submit(self._attempt, function, calls[index])] = index
                timeout = POLL_SECONDS
                if retries and len(in_flight) < concurrency:
                    timeout = min(timeout, max(0.0, retries[0][0] - now))
                done, _ = wait(in_flight, timeout=timeout, return_when=FIRST_COMPLETED) if in_flight else (set(), None)
                if not in_flight and timeout:
                    time.sleep(timeout)
                if time.monotonic() - last_poll >= POLL_SECONDS:
                    poll()
                    last_poll = time.monotonic()
                for future in done:
                    index = in_flight.pop(future)
                    outcome = future.result()
                    if outcome.fatal is not None:
                        raise outcome.fatal
                    if outcome.failure is None:
                        results[index].body = outcome.body
                        continue
                    failure, call = outcome.failure, calls[index]
                    fatal = self._fatal(function, failure)
                    if fatal is not None:
                        raise fatal
                    if failure.retryable and call.attempts < MAX_ATTEMPTS:
                        due = time.monotonic() + failure.wait_seconds(call.attempts)
                        if due <= call.started + REQUEST_BUDGET_SECONDS:
                            heapq.heappush(retries, (due, index))
                            continue
                    results[index].error = (
                        f"no answer after {call.attempts} attempts, the last {_describe(failure)}"
                        if failure.retryable
                        else _describe(failure)
                    )
            finished = True
        finally:
            pool.shutdown(wait=finished, cancel_futures=True)
        return results


# ---------------------------------------------------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------------------------------------------------

_sessions: dict[tuple[str, str, str, str], Session] = {}
_sessions_lock = threading.Lock()


def check_base_url(base_url: str) -> str:
    """The base URL without a trailing slash. A custom one receives the key and the rows, so it must be HTTPS, except
    on this machine."""
    parts = urlsplit(base_url)
    if parts.scheme == "https" and parts.hostname:
        return base_url.rstrip("/")
    if parts.scheme == "http" and (parts.hostname or "").lower() in LOCAL_HOSTS:
        return base_url.rstrip("/")
    raise DrexAuthError("NACE_BASE_URL must be an https:// URL (http:// only for localhost, 127.0.0.1 or [::1])")


def session() -> Session:
    """The session for the server environment's NACE_API_KEY and NACE_BASE_URL, read on every call so a changed
    environment takes effect in a new backend. Never a function argument or a setting: any user can read those.

    On an OpenRouter base URL the key is OPENROUTER_API_KEY when set, else NACE_API_KEY, and a `nace_` key is refused.
    NACE_OPENROUTER_REFERER and NACE_OPENROUTER_TITLE, when set, replace the app identity OpenRouter attributes
    requests to."""
    api_key = os.environ.get("NACE_API_KEY", "").strip()
    base_url = check_base_url(os.environ.get("NACE_BASE_URL", "").strip() or DEFAULT_BASE_URL)
    if is_open_router(base_url):
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip() or api_key
        if api_key.startswith("nace_"):
            raise DrexAuthError(
                "a Nace API key (nace_...) is never sent to OpenRouter; set OPENROUTER_API_KEY in the Postgres "
                "server's environment to an OpenRouter key and restart it"
            )
    if not api_key:
        raise DrexAuthError(
            "NACE_API_KEY is not set in the Postgres server's environment; set it where the server starts and restart it"
        )
    referer = os.environ.get("NACE_OPENROUTER_REFERER", "").strip()
    title = os.environ.get("NACE_OPENROUTER_TITLE", "").strip()
    key = (api_key, base_url, referer, title)
    with _sessions_lock:
        current = _sessions.get(key)
        if current is None:
            for stale in _sessions.values():
                stale.client.close()
            _sessions.clear()
            current = _sessions[key] = Session(api_key, base_url, referer, title)
        return current
