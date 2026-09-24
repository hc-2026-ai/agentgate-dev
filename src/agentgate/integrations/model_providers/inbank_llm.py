"""Inbank LLM SDK adapter for Judge model completions.

Wraps the ``abc_llm_sdk`` async streaming client behind the synchronous
``JudgeModelClient`` protocol.  The SDK import is deferred to
``__init__`` so that the ``api`` transport path never touches the binary
SDK, even when the package is absent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from agentgate.evaluator.judge import (
    JudgeModelTimeout,
    JudgeModelUnavailable,
    JudgeRequest,
    JudgeResponse,
)

LOGGER = logging.getLogger(__name__)


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value.strip()


class InbankLLMModelClient:
    """Adapt ``abc_llm_sdk.Client.AsyncOpenAI`` to ``JudgeModelClient``."""

    def __init__(
        self,
        *,
        provider_id: str,
        base_url: str,
        model_name: str,
        api_key: str,
        signature_hex: str,
        original_digest_hex: str,
        session_id: str,
    ) -> None:
        self.provider_id = _require_text(provider_id, "provider_id")
        self._base_url = _require_text(base_url, "base_url")
        self._model_name = _require_text(model_name, "model_name")
        self._api_key = _require_text(api_key, "api_key")
        self._signature_hex = _require_text(signature_hex, "signature_hex")
        self._original_digest_hex = _require_text(
            original_digest_hex, "original_digest_hex"
        )
        self._session_id = _require_text(session_id, "session_id")
        self._client = self._build_sdk_client()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(provider_id={self.provider_id!r}, "
            f"model_name={self._model_name!r})"
        )

    def complete(self, request: JudgeRequest) -> JudgeResponse:
        """Execute one bounded Judge completion request via the SDK."""

        messages: list[dict[str, Any]] = []
        if request.system_prompt is not None:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.user_prompt})

        create_kwargs: dict[str, Any] = {
            "messages": messages,
            "stream": True,
        }
        if request.max_output_tokens is not None:
            create_kwargs["max_tokens"] = request.max_output_tokens

        LOGGER.debug(
            "Inbank LLM Judge request: provider_id=%s, model=%s, timeout=%gs",
            self.provider_id,
            self._model_name,
            request.timeout_seconds,
        )
        started = time.monotonic()
        try:
            text = asyncio.run(self._collect_stream(create_kwargs, request.timeout_seconds))
        except TimeoutError as exc:
            LOGGER.warning(
                "Inbank LLM Judge timed out after %gs: provider_id=%s, model=%s",
                request.timeout_seconds,
                self.provider_id,
                self._model_name,
            )
            raise JudgeModelTimeout(
                f"Inbank LLM exceeded {request.timeout_seconds:g} seconds"
            ) from exc
        except Exception as exc:
            LOGGER.exception(
                "Inbank LLM Judge request failed: provider_id=%s, model=%s, error=%s",
                self.provider_id,
                self._model_name,
                type(exc).__name__,
            )
            raise JudgeModelUnavailable("Inbank LLM provider is unavailable") from exc

        latency_ms = (time.monotonic() - started) * 1000
        LOGGER.info(
            "Inbank LLM Judge response: provider_id=%s, model=%s, latency=%dms, chars=%d",
            self.provider_id,
            self._model_name,
            int(latency_ms),
            len(text),
        )
        return JudgeResponse(
            text=text,
            resolved_model_id=self._model_name,
            request_id=None,
            input_tokens=None,
            output_tokens=None,
            latency_ms=latency_ms,
            finish_reason=None,
            attempt_count=1,
        )

    def close(self) -> None:
        """Release SDK client resources if a close method is available."""

        close = getattr(self._client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                LOGGER.debug("Inbank LLM SDK close() failed", exc_info=True)

    async def _collect_stream(
        self,
        create_kwargs: dict[str, Any],
        timeout_seconds: float,
    ) -> str:
        """Run the async streaming call and collect text fragments."""

        stream = await self._client.chat.completions.create(**create_kwargs)

        fragments: list[str] = []
        try:
            async with asyncio.timeout(timeout_seconds):
                async for chunk in stream:
                    if chunk:
                        fragments.append(str(chunk))
        finally:
            if hasattr(stream, "aclose"):
                try:
                    await stream.aclose()
                except Exception:
                    LOGGER.debug("Inbank LLM stream aclose() failed", exc_info=True)

        return "".join(fragments)

    def _build_sdk_client(self) -> Any:
        """Import and construct the SDK client; deferred so ``api`` mode never imports."""

        try:
            from abc_llm_sdk import Client  # type: ignore[import-not-found]
        except ImportError as exc:
            LOGGER.error(
                "abc_llm_sdk import failed; AGENTGATE_JUDGE_TRANSPORT=sdk requires the SDK: %s",
                type(exc).__name__,
            )
            raise ImportError(
                "abc_llm_sdk is required for AGENTGATE_JUDGE_TRANSPORT=sdk; "
                "install the SDK or switch to transport=api"
            ) from exc

        LOGGER.info(
            "Building Inbank LLM SDK client: provider_id=%s, model=%s, base_url=%s",
            self.provider_id,
            self._model_name,
            self._base_url,
        )
        return Client.AsyncOpenAI(
            api_key=self._api_key,
            model_name=self._model_name,
            base_url=self._base_url,
            signature_hex=self._signature_hex,
            original_digest_hex=self._original_digest_hex,
            session_id=self._session_id,
        )


__all__ = ["InbankLLMModelClient"]
