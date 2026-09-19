"""Cancellable sentence/stream orchestration for local TTS providers."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable, Iterator

from .audio import write_wav
from .contracts import (
    AudioChunk,
    TTSCancelled,
    TTSConfig,
    TTSRequest,
    TTSResult,
    TTSRun,
    TTSStreamEvent,
    TTSError,
    TTSUnavailable,
)
from .registry import TTSProvider, TTSProviderRegistry, default_registry
from .sentence import split_sentences


_END = object()
ChunkCallback = Callable[[AudioChunk], Awaitable[None]]


def _next_or_end(iterator: Iterator[AudioChunk]):
    try:
        return next(iterator)
    except StopIteration:
        return _END


class TTSService:
    """Owns one local provider instance and keeps the B05 boundary intact."""

    def __init__(self, config: TTSConfig, *, registry: TTSProviderRegistry | None = None) -> None:
        self.config = config
        self.registry = registry or default_registry()
        self._provider: TTSProvider | None = None
        self._provider_name: str | None = None

    def health(self) -> dict[str, object]:
        if not self.config.enabled:
            return {
                "status": "disabled",
                "provider": self.config.provider,
                "reason_code": "TTS_DISABLED",
                "fallback": self.config.fallback,
            }
        try:
            provider = self._get_provider()
            result = dict(provider.health())
            result["fallback"] = self.config.fallback
            return result
        except TTSUnavailable as exc:
            return {
                "status": "unavailable",
                "provider": self.config.provider,
                "reason_code": exc.code,
                "fallback": self.config.fallback,
            }

    def _get_provider(self) -> TTSProvider:
        if self._provider is None or self._provider_name != self.config.provider:
            if self._provider is not None:
                self._provider.close()
            self._provider = self.registry.create(self.config)
            self._provider_name = self.config.provider
        return self._provider

    async def stream(self, request: TTSRequest):
        """Yield real audio chunks in sentence/chunk order."""

        request.validate(self.config.max_input_chars)
        if not self.config.enabled:
            raise TTSUnavailable("TTS_DISABLED")
        provider = self._get_provider()
        health = provider.health()
        if health.get("status") != "available":
            raise TTSUnavailable(str(health.get("reason_code", "TTS_UNAVAILABLE")))
        sentences = split_sentences(request.text, max_chars=self.config.max_input_chars)
        for sentence_index, sentence in enumerate(sentences):
            if request.cancel_event.is_set():
                raise TTSCancelled()
            iterator = provider.stream_sentence(sentence, request, sentence_index)
            try:
                while True:
                    if request.cancel_event.is_set():
                        raise TTSCancelled()
                    chunk = await asyncio.to_thread(_next_or_end, iterator)
                    if chunk is _END:
                        break
                    if not isinstance(chunk, AudioChunk) or not chunk.sample_count:
                        continue
                    yield chunk
            finally:
                close_iterator = getattr(iterator, "close", None)
                if close_iterator is not None:
                    try:
                        await asyncio.to_thread(close_iterator)
                    except Exception:
                        pass

    async def synthesize(
        self,
        request: TTSRequest,
        *,
        output_path: str | Path | None = None,
        on_chunk: ChunkCallback | None = None,
    ) -> TTSResult:
        """Collect a complete request, writing a WAV only after completion."""

        started = time.perf_counter()
        try:
            request.validate(self.config.max_input_chars)
            sentences = split_sentences(request.text, max_chars=self.config.max_input_chars)
        except TTSError as exc:
            return self._failure(request, started, "failed", exc.code, 0, None, None)

        chunks: list[AudioChunk] = []
        first_audio_ms: float | None = None
        sample_rate: int | None = None
        try:
            async for chunk in self.stream(request):
                if first_audio_ms is None:
                    first_audio_ms = (time.perf_counter() - started) * 1000.0
                sample_rate = sample_rate or chunk.sample_rate
                chunks.append(chunk)
                if on_chunk is not None:
                    await on_chunk(chunk)
            if request.cancel_event.is_set():
                raise TTSCancelled()
            if not chunks or sample_rate is None:
                raise TTSUnavailable("TTS_EMPTY_AUDIO")
            target = str(output_path) if output_path is not None else None
            sample_count = sum(chunk.sample_count for chunk in chunks)
            if target is not None:
                write_wav(target, sample_rate, (chunk.samples for chunk in chunks))
            ended_ms = (time.perf_counter() - started) * 1000.0
            return TTSResult(
                request_id=request.request_id,
                status="completed",
                provider=self.config.provider,
                sentence_count=len(sentences),
                chunk_count=len(chunks),
                sample_rate=sample_rate,
                sample_count=sample_count,
                duration_seconds=sample_count / sample_rate,
                first_audio_ms=first_audio_ms,
                ended_ms=ended_ms,
                output_path=target,
            )
        except TTSCancelled as exc:
            return self._fallback_or_error(
                request,
                started,
                "cancelled",
                exc.code,
                len(sentences),
                first_audio_ms,
            )
        except TTSUnavailable as exc:
            return self._fallback_or_error(
                request,
                started,
                "unavailable",
                exc.code,
                len(sentences),
                first_audio_ms,
            )
        except asyncio.CancelledError:
            request.cancel()
            return self._fallback_or_error(
                request,
                started,
                "cancelled",
                "TTS_CANCELLED",
                len(sentences),
                first_audio_ms,
            )
        except Exception:
            return self._fallback_or_error(
                request,
                started,
                "failed",
                "TTS_INTERNAL",
                len(sentences),
                first_audio_ms,
            )

    def _fallback_or_error(
        self,
        request: TTSRequest,
        started: float,
        status: str,
        code: str,
        sentence_count: int,
        first_audio_ms: float | None,
    ) -> TTSResult:
        ended_ms = (time.perf_counter() - started) * 1000.0
        if status == "unavailable" and self.config.fallback == "text":
            return TTSResult(
                request_id=request.request_id,
                status="text_fallback",
                provider=self.config.provider,
                sentence_count=sentence_count,
                first_audio_ms=first_audio_ms,
                ended_ms=ended_ms,
                fallback_text=request.text,
                error_code=code,
            )
        return TTSResult(
            request_id=request.request_id,
            status=status,
            provider=self.config.provider,
            sentence_count=sentence_count,
            first_audio_ms=first_audio_ms,
            ended_ms=ended_ms,
            error_code=code,
        )

    def _failure(
        self,
        request: TTSRequest,
        started: float,
        status: str,
        code: str,
        sentence_count: int,
        first_audio_ms: float | None,
        sample_rate: int | None,
    ) -> TTSResult:
        return TTSResult(
            request_id=request.request_id,
            status=status,
            provider=self.config.provider,
            sentence_count=sentence_count,
            first_audio_ms=first_audio_ms,
            ended_ms=(time.perf_counter() - started) * 1000.0,
            sample_rate=sample_rate,
            error_code=code,
        )

    async def start(self, request: TTSRequest, *, output_path: str | Path | None = None) -> TTSRun:
        run = TTSRun(request)
        run.task = asyncio.create_task(self._run(run, output_path=output_path), name=f"tts-{request.request_id}")
        return run

    async def _run(self, run: TTSRun, *, output_path: str | Path | None) -> TTSResult:
        started_ms = (time.perf_counter() - run.request.started_at) * 1000.0
        await run.queue.put(TTSStreamEvent(run.request.request_id, "accepted", started_ms))

        async def publish_chunk(chunk: AudioChunk) -> None:
            timestamp_ms = (time.perf_counter() - run.request.started_at) * 1000.0
            await run.queue.put(TTSStreamEvent(run.request.request_id, "audio_chunk", timestamp_ms, chunk=chunk))

        result = await self.synthesize(run.request, output_path=output_path, on_chunk=publish_chunk)
        if not run._result.done():
            run._result.set_result(result)
        event = result.status if result.status in {"completed", "cancelled", "unavailable", "text_fallback", "failed"} else "failed"
        await run.queue.put(
            TTSStreamEvent(
                run.request.request_id,
                event,
                result.ended_ms or 0.0,
                result=result,
                error_code=result.error_code,
            )
        )
        return result

    def close(self) -> None:
        if self._provider is not None:
            self._provider.close()
            self._provider = None
            self._provider_name = None
