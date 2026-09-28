from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentgate.evaluator.judge import (
    JudgeModelInvalidResponse,
    JudgeModelTimeout,
    JudgeModelUnavailable,
    JudgeRequest,
)
from agentgate.integrations.model_providers.inbank_llm import (
    InbankLLMModelClient,
    _extract_chunk_text,
)


def _make_request(**kwargs: Any) -> JudgeRequest:
    defaults: dict[str, Any] = {
        "model_id": "test-model",
        "user_prompt": "hello",
        "system_prompt": None,
        "temperature": 0.0,
        "seed": None,
        "max_output_tokens": 100,
        "response_format": "json_object",
        "timeout_seconds": 60.0,
    }
    defaults.update(kwargs)
    return JudgeRequest(**defaults)


def _patch_build_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
    fake_client: Any,
) -> None:
    monkeypatch.setattr(
        "agentgate.integrations.model_providers.inbank_llm.InbankLLMModelClient._build_sdk_client",
        lambda self: fake_client,
    )


def _make_client() -> InbankLLMModelClient:
    return InbankLLMModelClient(
        provider_id="test-provider",
        base_url="http://test.example",
        model_name="test-model",
        api_key="test-key",
        signature_hex="test-sig",
        original_digest_hex="test-digest",
        session_id="test-session",
    )


def _delta_chunk(content: str | None) -> Any:
    """One streaming chunk carrying delta text."""

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content),
                finish_reason=None,
            )
        ]
    )


def _finish_chunk(reason: str) -> Any:
    """One streaming chunk carrying only a finish reason."""

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None),
                finish_reason=reason,
            )
        ]
    )


def _usage_chunk() -> Any:
    """One usage-only chunk without choices, sent at the end of some streams."""

    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
    )


# ---------------------------------------------------------------------------
# Construction tests
# ---------------------------------------------------------------------------


def test_init_raises_importerror_when_sdk_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "abc_llm_sdk", None)

    with pytest.raises(ImportError, match="abc_llm_sdk"):
        InbankLLMModelClient(
            provider_id="p",
            base_url="http://t.example",
            model_name="m",
            api_key="k",
            signature_hex="s",
            original_digest_hex="d",
            session_id="sid",
        )


# ---------------------------------------------------------------------------
# complete() tests
# ---------------------------------------------------------------------------


def test_complete_collects_stream_fragments(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        _delta_chunk("Hello"),
        _delta_chunk(", "),
        _delta_chunk("World"),
        _usage_chunk(),
        _finish_chunk("stop"),
    ]
    fake_client = _FakeClient(_MockStream(chunks))
    _patch_build_sdk_client(monkeypatch, fake_client)

    client = _make_client()
    request = _make_request(user_prompt="Evaluate this")

    response = client.complete(request)

    assert response.text == "Hello, World"
    assert response.resolved_model_id == "test-model"
    assert response.request_id is None
    assert response.input_tokens is None
    assert response.output_tokens is None
    assert response.latency_ms is not None
    assert response.latency_ms >= 0
    assert response.finish_reason == "stop"
    assert response.truncated is False
    assert response.attempt_count == 1
    client.close()


def test_complete_passes_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _MockStream([_delta_chunk("ok"), _finish_chunk("stop")])

    fake_client = _FakeClientWithCallback(fake_create)
    _patch_build_sdk_client(monkeypatch, fake_client)

    client = _make_client()
    request = _make_request(max_output_tokens=500)

    client.complete(request)

    assert captured["max_tokens"] == 500
    assert captured["stream"] is True
    assert "temperature" not in captured
    assert "seed" not in captured
    assert "response_format" not in captured
    client.close()


def test_complete_includes_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _MockStream([_delta_chunk("ok"), _finish_chunk("stop")])

    fake_client = _FakeClientWithCallback(fake_create)
    _patch_build_sdk_client(monkeypatch, fake_client)

    client = _make_client()
    request = _make_request(system_prompt="system instruction")

    client.complete(request)

    messages = captured["messages"]
    assert messages[0] == {"role": "system", "content": "system instruction"}
    assert messages[1]["role"] == "user"
    client.close()


def test_complete_raises_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeClient(_SlowStream())
    _patch_build_sdk_client(monkeypatch, fake_client)

    client = _make_client()
    request = _make_request(timeout_seconds=0.05)

    with pytest.raises(JudgeModelTimeout):
        client.complete(request)
    client.close()


def test_complete_raises_unavailable_on_sdk_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raising_create(**kwargs: Any) -> Any:
        raise RuntimeError("simulated SDK failure")

    fake_client = _FakeClientWithCallback(_raising_create)
    _patch_build_sdk_client(monkeypatch, fake_client)

    client = _make_client()
    request = _make_request()

    with pytest.raises(JudgeModelUnavailable):
        client.complete(request)
    client.close()


# ---------------------------------------------------------------------------
# Stream chunk handling tests
# ---------------------------------------------------------------------------


def test_complete_marks_truncated_response(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [_delta_chunk("partial answer"), _finish_chunk("length")]
    fake_client = _FakeClient(_MockStream(chunks))
    _patch_build_sdk_client(monkeypatch, fake_client)

    client = _make_client()
    request = _make_request()

    response = client.complete(request)

    assert response.text == "partial answer"
    assert response.finish_reason == "length"
    assert response.truncated is True
    client.close()


def test_complete_skips_usage_only_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [_delta_chunk("ok"), _usage_chunk(), _delta_chunk("!"), _finish_chunk("stop")]
    fake_client = _FakeClient(_MockStream(chunks))
    _patch_build_sdk_client(monkeypatch, fake_client)

    client = _make_client()
    request = _make_request()

    response = client.complete(request)

    assert response.text == "ok!"
    assert response.finish_reason == "stop"
    client.close()


def test_complete_raises_invalid_response_on_empty_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [_usage_chunk()]
    fake_client = _FakeClient(_MockStream(chunks))
    _patch_build_sdk_client(monkeypatch, fake_client)

    client = _make_client()
    request = _make_request()

    with pytest.raises(JudgeModelInvalidResponse, match="stream without text content"):
        client.complete(request)
    client.close()


@pytest.mark.parametrize(
    ("chunk", "expected_text", "expected_reason"),
    [
        (_delta_chunk("hello"), "hello", None),
        (_finish_chunk("stop"), "", "stop"),
        (_usage_chunk(), "", None),
        (
            SimpleNamespace(choices=[SimpleNamespace(delta=None, finish_reason="stop")]),
            "",
            "stop",
        ),
        (
            SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason=None)
                ]
            ),
            "",
            None,
        ),
        (
            SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content=123), finish_reason=None)
                ]
            ),
            "",
            None,
        ),
        (
            SimpleNamespace(
                choices=[
                    SimpleNamespace(delta=SimpleNamespace(content="text"), finish_reason=42)
                ]
            ),
            "text",
            None,
        ),
    ],
)
def test_extract_chunk_text(chunk: Any, expected_text: str, expected_reason: str | None) -> None:
    assert _extract_chunk_text(chunk) == (expected_text, expected_reason)


# ---------------------------------------------------------------------------
# close() tests
# ---------------------------------------------------------------------------


def test_close_calls_sdk_close_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = MagicMock()
    _patch_build_sdk_client(monkeypatch, fake_client)

    client = _make_client()
    client.close()

    fake_client.close.assert_called_once()


def test_close_silent_when_no_close_method(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_build_sdk_client(monkeypatch, object())

    client = _make_client()
    client.close()


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _MockStream:
    """Async stream that yields the given chunks."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self) -> _MockStream:
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        pass


class _SlowStream:
    """Async stream that sleeps longer than the timeout on first iteration."""

    def __aiter__(self) -> _SlowStream:
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(0.2)
        raise StopAsyncIteration

    async def aclose(self) -> None:
        pass


class _FakeClient:
    """Minimal fake of abc_llm_sdk.Client.AsyncOpenAI."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    @property
    def chat(self) -> _FakeChat:
        return _FakeChat(self._stream)


class _FakeChat:
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    @property
    def completions(self) -> _FakeCompletions:
        return _FakeCompletions(self._stream)


class _FakeCompletions:
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def create(self, **kwargs: Any) -> Any:
        return self._stream


class _FakeClientWithCallback:
    """Fake client that captures create() kwargs for assertions."""

    def __init__(self, create_fn: Any) -> None:
        self._create_fn = create_fn

    @property
    def chat(self) -> _FakeChatWithCallback:
        return _FakeChatWithCallback(self._create_fn)


class _FakeChatWithCallback:
    def __init__(self, create_fn: Any) -> None:
        self._create_fn = create_fn

    @property
    def completions(self) -> _FakeCompletionsWithCallback:
        return _FakeCompletionsWithCallback(self._create_fn)


class _FakeCompletionsWithCallback:
    def __init__(self, create_fn: Any) -> None:
        self._create_fn = create_fn

    async def create(self, **kwargs: Any) -> Any:
        return self._create_fn(**kwargs)
