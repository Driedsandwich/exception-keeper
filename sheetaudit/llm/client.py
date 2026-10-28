"""Chat interface. Wire format: https://docs.tokenfactory.nebius.com/quickstart.md

urllib API signatures were checked in the installed Python sources.
Never retain credentials or provider error bodies. Redirects are disabled.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import os
import time
from typing import Protocol
from urllib import error, request
from urllib.parse import urlsplit

from .models import BASE_URL


def redact(value, key=None):
    if key is None:
        for name in ("NEBIUS_API_KEY", "EK_JUDGE_PASSPHRASE"):
            secret = os.environ.get(name)
            if secret:
                value = redact(value, secret)
        return value
    if isinstance(value, str):
        return value.replace(key, "[redacted]") if key else value
    if isinstance(value, dict):
        return {redact(k, key): redact(v, key) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact(v, key) for v in value]
    return value


class LLMError(Exception):
    def __init__(self, status, short_message):
        self.status = status
        self.short_message = redact(short_message)[:160]
        super().__init__(status, self.short_message)

    def __str__(self):
        return f"LLM request failed ({self.status}): {self.short_message}"


@dataclass
class ChatResult:
    content: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict | None = None
    model: str = ""
    rate_headers: dict = field(default_factory=dict)
    attempts: int = 1
    finish_reason: str | None = None


class ChatClient(Protocol):
    def complete(self, model, messages, *, tools=None, tool_choice=None,
                 response_format=None, max_tokens=1024, temperature=0) -> ChatResult: ...


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpChatClient:
    def __init__(self, base_url=BASE_URL, api_key_env="NEBIUS_API_KEY",
                 timeout_s=30, max_retries=2):
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("base_url must be an HTTP(S) URL without credentials")
        if timeout_s <= 0 or not isinstance(max_retries, int) or not 0 <= max_retries <= 5:
            raise ValueError("Invalid timeout or retry count")
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s  # Total wall-clock budget, including retry waits.
        self.max_retries = max_retries

    def complete(self, model, messages, *, tools=None, tool_choice=None,
                 response_format=None, max_tokens=1024, temperature=0):
        key = os.environ.get(self.api_key_env)
        if not key:
            raise LLMError(None, "Missing API key environment variable")
        payload = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
        for name, value in (("tools", tools), ("tool_choice", tool_choice),
                            ("response_format", response_format)):
            if value is not None:
                payload[name] = value
        data = json.dumps(payload, ensure_ascii=False).encode()
        opener = request.build_opener(_NoRedirect())
        deadline = time.monotonic() + self.timeout_s
        for attempt in range(self.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LLMError(None, "Total request time budget exhausted")
            req = request.Request(self.base_url + "chat/completions", data=data,
                                  headers={"Authorization": "Bearer " + key,
                                           "Content-Type": "application/json"}, method="POST")
            try:
                with opener.open(req, timeout=remaining) as response:
                    # Bound provider response size; error bodies are never read.
                    raw = response.read(1_000_001)
                    headers = {k.lower(): v for k, v in response.headers.items()
                               if k.lower().startswith("x-ratelimit-") or k.lower() == "retry-after"}
                if len(raw) > 1_000_000:
                    raise LLMError(None, "Response exceeds size limit")
                body = redact(json.loads(raw), key)
                choice = body["choices"][0]
                message = choice["message"]
                if not isinstance(message, dict):
                    raise LLMError(None, "Malformed chat message")
                content = message.get("content")
                calls = message.get("tool_calls") or []
                if (content is not None and not isinstance(content, str)) or not isinstance(calls, list):
                    raise LLMError(None, "Malformed chat message")
                return ChatResult(content, calls, body.get("usage"), body.get("model", model),
                                  redact(headers, key), attempt + 1,
                                  choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None)
            except error.HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After", "0")
                exc.close()
                if status != 429 and not 500 <= status <= 599:
                    raise LLMError(status, "HTTP request rejected") from None
                if attempt == self.max_retries:
                    raise LLMError(status, "Retry limit reached") from None
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        delay = 0
                delay = min(2.0, max(0.0, delay))
                if delay >= deadline - time.monotonic():
                    raise LLMError(status, "Total request time budget exhausted") from None
                time.sleep(delay)
            except LLMError:
                raise
            except (error.URLError, OSError, HTTPException, ValueError, KeyError,
                    IndexError, TypeError, RecursionError):
                raise LLMError(None, "Transport failure or malformed response") from None


class MockChatClient:
    def __init__(self, script):
        responses = deepcopy(script.get("responses", []) if isinstance(script, dict) else script)
        self._script = iter(responses)
        self._keyed = {}
        for item in responses:
            if isinstance(item, dict) and "stage" in item:
                key = (item["stage"], item.get("candidate_id"))
                self._keyed.setdefault(key, []).append(item)
        self.requests = []

    def for_stage(self, stage, candidate_id=None):
        """An independent round/repair cursor for one stage and candidate.

        Unkeyed lists remain useful for isolated transport and repair tests.
        """
        if not self._keyed:
            return self
        bound = MockChatClient(self._keyed.get((stage, candidate_id), []))
        bound.requests = self.requests
        return bound

    def complete(self, model, messages, *, tools=None, tool_choice=None,
                 response_format=None, max_tokens=1024, temperature=0):
        self.requests.append(redact(deepcopy(dict(model=model, messages=messages, tools=tools,
                             tool_choice=tool_choice, response_format=response_format,
                             max_tokens=max_tokens, temperature=temperature))))
        try:
            item = next(self._script)
        except StopIteration:
            raise LLMError(None, "Mock script exhausted") from None
        if isinstance(item, ChatResult):
            return ChatResult(**redact(vars(item)))
        item = redact(item)
        if "error" in item:
            raise LLMError(item.get("status"), item["error"])
        return ChatResult(content=item.get("content"), tool_calls=item.get("tool_calls", []),
                          usage=item.get("usage"), model=item.get("model", model),
                          rate_headers=item.get("rate_headers", {}))
