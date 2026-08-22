"""Deterministic, local-only media playback state machine."""

from __future__ import annotations

import asyncio
import inspect
import math
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Protocol, cast

from .contracts import (
    ASSET_REF_RE,
    REQUEST_ID_RE,
    TRACK_ID_RE,
    Action,
    AssetKind,
    AssetStatus,
    FallbackPolicy,
    MediaCommand,
    MediaEvent,
    MediaSnapshot,
    MediaStateError,
    MusicCatalog,
    OperationResult,
    OperationStatus,
    PlaybackStatus,
    SceneState,
    TimeOfDay,
    TrackDefinition,
)
from .resolver import ResolvedAsset


class AssetResolver(Protocol):
    def resolve(self, asset_ref: str, kind: AssetKind) -> ResolvedAsset:
        """Return a local manifest-backed asset or raise a coded error."""


class MediaProvider(Protocol):
    async def set_source(
        self,
        audio: ResolvedAsset,
        visual: ResolvedAsset | None,
        *,
        position_seconds: float,
        playing: bool,
    ) -> None:
        """Stage/replace original assets; commit atomically or raise."""

    async def pause(self) -> None:
        """Pause the active source."""

    async def stop(self) -> None:
        """Stop output and release the active source."""

    async def seek(self, position_seconds: float) -> None:
        """Seek the active source."""

    async def set_visual(self, visual: ResolvedAsset) -> None:
        """Switch to an original visual asset without replacing audio."""


EventSink = Callable[[MediaEvent], Any]


class _OperationCanceled(Exception):
    pass


@dataclass
class _Operation:
    operation_id: str
    command: MediaCommand
    request_id: str | None
    status: OperationStatus = OperationStatus.PENDING
    result: OperationResult | None = None
    task: asyncio.Task[OperationResult] | None = None
    cancel_requested: bool = False
    committed_revision: int | None = None


class OperationHandle:
    """A stable handle for polling, waiting, cancellation, and retry."""

    def __init__(self, machine: "MediaStateMachine", operation_id: str) -> None:
        self._machine = machine
        self.operation_id = operation_id

    async def wait(self) -> OperationResult:
        return await self._machine.wait(self.operation_id)

    def status(self) -> OperationResult:
        return self._machine.status(self.operation_id)


class MediaStateMachine:
    """Serialize commands and commit state only after local provider success.

    No default provider is created.  A caller that wants real playback must
    inject a provider that consumes ``ResolvedAsset`` values and implements
    cancellation-safe, local playback semantics.
    """

    _PROVIDER_FAILURES = {
        "PLAYBACK_PROVIDER_UNAVAILABLE",
        "PROVIDER_ERROR",
        "PROVIDER_UNAVAILABLE",
    }

    def __init__(
        self,
        catalog: MusicCatalog,
        resolver: AssetResolver | None,
        provider: MediaProvider | None,
        *,
        fallback_policy: FallbackPolicy = FallbackPolicy.ERROR,
        event_sink: EventSink | None = None,
    ) -> None:
        if not isinstance(catalog, MusicCatalog):
            raise MediaStateError("INVALID_CATALOG")
        try:
            fallback_policy = FallbackPolicy(fallback_policy)
        except ValueError as exc:
            raise MediaStateError("INVALID_FALLBACK_POLICY") from exc
        self._catalog = catalog
        self._resolver = resolver
        self._provider = provider
        self._fallback_policy = fallback_policy
        self._event_sink = event_sink
        self._snapshot = MediaSnapshot()
        self._lock = asyncio.Lock()
        self._operations: dict[str, _Operation] = {}
        self._request_index: dict[str, str] = {}
        self._next_operation = 1

    def snapshot(self) -> MediaSnapshot:
        return self._snapshot

    def submit(self, command: MediaCommand, *, request_id: str | None = None) -> OperationHandle:
        if not isinstance(command, MediaCommand):
            raise MediaStateError("INVALID_COMMAND")
        if request_id is not None and (
            not isinstance(request_id, str) or REQUEST_ID_RE.fullmatch(request_id) is None
        ):
            raise MediaStateError("INVALID_REQUEST_ID")
        if request_id is not None and request_id in self._request_index:
            existing = self._operations[self._request_index[request_id]]
            if existing.command != command:
                raise MediaStateError("REQUEST_ID_REUSED")
            return OperationHandle(self, existing.operation_id)

        operation_id = f"media-op-{self._next_operation:04d}"
        self._next_operation += 1
        operation = _Operation(operation_id, command, request_id)
        self._operations[operation_id] = operation
        if request_id is not None:
            self._request_index[request_id] = operation_id
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            self._operations.pop(operation_id, None)
            if request_id is not None:
                self._request_index.pop(request_id, None)
            raise MediaStateError("EVENT_LOOP_REQUIRED") from exc
        operation.task = loop.create_task(self._run(operation), name=operation_id)
        return OperationHandle(self, operation_id)

    def status(self, operation_id: str) -> OperationResult:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise MediaStateError("OPERATION_NOT_FOUND")
        if operation.result is not None:
            return operation.result
        return OperationResult(
            operation_id=operation.operation_id,
            action=operation.command.action,
            status=operation.status,
            snapshot=self._snapshot,
        )

    async def wait(self, operation_id: str) -> OperationResult:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise MediaStateError("OPERATION_NOT_FOUND")
        if operation.result is not None:
            return operation.result
        if operation.task is None:
            raise MediaStateError("OPERATION_NOT_FOUND")
        try:
            return await asyncio.shield(operation.task)
        except asyncio.CancelledError:
            if operation.result is not None:
                return operation.result
            raise

    async def cancel(self, operation_id: str) -> OperationResult:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise MediaStateError("OPERATION_NOT_FOUND")
        if operation.result is not None:
            return operation.result
        operation.cancel_requested = True
        # Let a freshly scheduled PENDING task enter _run first.  Then cancel
        # the task even if it is waiting for _lock; otherwise a queued request
        # can remain blocked behind an unrelated provider call forever.
        if operation.status == OperationStatus.PENDING:
            await asyncio.sleep(0)
        if operation.result is None and operation.task is not None and not operation.task.done():
            operation.task.cancel()
        return await self.wait(operation_id)

    def retry(self, operation_id: str) -> OperationHandle:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise MediaStateError("OPERATION_NOT_FOUND")
        if operation.result is None or operation.result.status not in {
            OperationStatus.FAILED,
            OperationStatus.CANCELED,
        }:
            raise MediaStateError("RETRY_NOT_AVAILABLE")
        request_id = None
        if operation.request_id is not None:
            request_id = f"{operation.request_id}:retry:{self._next_operation}"
        return self.submit(operation.command, request_id=request_id)

    async def _emit(self, event: MediaEvent) -> None:
        if self._event_sink is None:
            return
        try:
            result = self._event_sink(event)
            if inspect.isawaitable(result):
                await cast(Awaitable[Any], result)
        except Exception:
            # Observability must not turn a successful local playback operation
            # into a false failure, and event payloads contain no sensitive data.
            return

    async def _run(self, operation: _Operation) -> OperationResult:
        operation.status = OperationStatus.RUNNING
        before = self._snapshot
        try:
            await self._emit(
                MediaEvent(
                    event_type="operation_started",
                    operation_id=operation.operation_id,
                    action=operation.command.action,
                    status=OperationStatus.RUNNING,
                    snapshot=before,
                )
            )
            async with self._lock:
                before = self._snapshot
                try:
                    self._check_cancel(operation)
                    changed = await self._execute(operation)
                except (_OperationCanceled, asyncio.CancelledError):
                    # Provider calls are the transaction boundary: all state
                    # transitions happen after their await succeeds.  A
                    # canceled operation therefore has no own state to roll
                    # back.  In particular, never restore ``before`` here;
                    # another operation may have committed since that
                    # state value was captured.
                    result = OperationResult(
                        operation_id=operation.operation_id,
                        action=operation.command.action,
                        status=OperationStatus.CANCELED,
                        snapshot=self._snapshot,
                    )
                except MediaStateError as exc:
                    if exc.code in self._PROVIDER_FAILURES:
                        self._transition(
                            playback=PlaybackStatus.ERROR,
                            last_error_code=exc.code,
                        )
                    else:
                        self._transition(last_error_code=exc.code)
                    if self._snapshot.revision != before.revision:
                        operation.committed_revision = self._snapshot.revision
                    result = OperationResult(
                        operation_id=operation.operation_id,
                        action=operation.command.action,
                        status=OperationStatus.FAILED,
                        snapshot=self._snapshot,
                        error_code=exc.code,
                        retryable=exc.retryable,
                    )
                except Exception:
                    self._transition(
                        playback=PlaybackStatus.ERROR,
                        last_error_code="INTERNAL_ERROR",
                    )
                    if self._snapshot.revision != before.revision:
                        operation.committed_revision = self._snapshot.revision
                    result = OperationResult(
                        operation_id=operation.operation_id,
                        action=operation.command.action,
                        status=OperationStatus.FAILED,
                        snapshot=self._snapshot,
                        error_code="INTERNAL_ERROR",
                        retryable=False,
                    )
                else:
                    if self._snapshot.revision != before.revision:
                        operation.committed_revision = self._snapshot.revision
                    if self._snapshot.last_error_code is not None:
                        if self._transition(last_error_code=None):
                            operation.committed_revision = self._snapshot.revision
                    result = OperationResult(
                        operation_id=operation.operation_id,
                        action=operation.command.action,
                        status=(
                            OperationStatus.COMPLETED
                            if changed
                            else OperationStatus.NOOP
                        ),
                        snapshot=self._snapshot,
                    )
        except asyncio.CancelledError:
            operation.cancel_requested = True
            # Cancellation while waiting for ``_lock`` or while emitting an
            # event cannot undo a later/parallel commit.  The provider/state
            # transaction has not been committed by this operation, so report
            # the current state without writing an earlier value back.
            if operation.result is not None:
                return operation.result
            result = OperationResult(
                operation_id=operation.operation_id,
                action=operation.command.action,
                status=OperationStatus.CANCELED,
                snapshot=self._snapshot,
            )

        operation.result = result
        operation.status = result.status
        if operation.committed_revision is not None:
            await self._emit(
                MediaEvent(
                    event_type="state_changed",
                    operation_id=operation.operation_id,
                    action=operation.command.action,
                    status=result.status,
                    snapshot=result.snapshot,
                    error_code=result.error_code,
                )
            )
        await self._emit(
            MediaEvent(
                event_type="operation_finished",
                operation_id=operation.operation_id,
                action=operation.command.action,
                status=result.status,
                snapshot=result.snapshot,
                error_code=result.error_code,
            )
        )
        return result

    def _check_cancel(self, operation: _Operation) -> None:
        if operation.cancel_requested:
            raise _OperationCanceled

    def _transition(self, **changes: Any) -> bool:
        current = self._snapshot
        candidate = replace(current, **changes)
        comparable_current = replace(current, revision=0)
        comparable_candidate = replace(candidate, revision=0)
        if comparable_current == comparable_candidate:
            return False
        self._snapshot = replace(candidate, revision=current.revision + 1)
        return True

    def _track(self, track_id: str | None) -> TrackDefinition:
        if not isinstance(track_id, str) or TRACK_ID_RE.fullmatch(track_id) is None:
            raise MediaStateError("INVALID_TRACK_ID")
        return self._catalog.get(track_id)

    def _scene_for_command(self, command: MediaCommand) -> SceneState:
        if command.time_of_day is None and command.performance is None:
            raise MediaStateError("INVALID_SCENE_STATE")
        return SceneState(
            time_of_day=command.time_of_day or self._snapshot.time_of_day,
            performance=command.performance or self._snapshot.performance,
        )

    def _resolve(self, reference: str, kind: AssetKind) -> ResolvedAsset:
        if not isinstance(reference, str) or ASSET_REF_RE.fullmatch(reference) is None:
            raise MediaStateError("INVALID_ASSET_REFERENCE")
        if self._resolver is None:
            raise MediaStateError("ASSET_RESOLVER_UNAVAILABLE", retryable=True)
        try:
            return self._resolver.resolve(reference, kind)
        except MediaStateError:
            raise
        except Exception as exc:
            raise MediaStateError("ASSET_RESOLVER_ERROR", retryable=True) from exc

    def _resolve_audio(self, track: TrackDefinition) -> tuple[ResolvedAsset, bool]:
        try:
            return self._resolve(track.audio_asset_ref, AssetKind.AUDIO), False
        except MediaStateError as primary:
            if (
                self._fallback_policy != FallbackPolicy.USE_DECLARED_FALLBACK
                or track.fallback_audio_asset_ref is None
            ):
                raise primary
            try:
                return self._resolve(track.fallback_audio_asset_ref, AssetKind.AUDIO), True
            except MediaStateError as exc:
                raise MediaStateError("ASSET_FALLBACK_UNAVAILABLE") from exc

    def _resolve_visual(
        self,
        track: TrackDefinition,
        scene: SceneState,
        *,
        required: bool,
    ) -> tuple[ResolvedAsset | None, bool]:
        reference = track.visual_asset_refs.get(scene.key)
        if reference is None:
            if not required:
                return None, False
            primary = MediaStateError("ASSET_NOT_FOUND")
        else:
            try:
                return self._resolve(reference, AssetKind.VIDEO), False
            except MediaStateError as exc:
                primary = exc

        if self._fallback_policy == FallbackPolicy.USE_DECLARED_FALLBACK:
            fallback_ref = track.fallback_visual_asset_refs.get(scene.key)
            if fallback_ref is not None:
                try:
                    return self._resolve(fallback_ref, AssetKind.VIDEO), True
                except MediaStateError as exc:
                    raise MediaStateError("ASSET_FALLBACK_UNAVAILABLE") from exc
        if self._fallback_policy == FallbackPolicy.SILENT:
            return None, True
        raise primary

    def _provider_method(self, name: str) -> Callable[..., Any]:
        if self._provider is None:
            raise MediaStateError("PLAYBACK_PROVIDER_UNAVAILABLE", retryable=True)
        method = getattr(self._provider, name, None)
        if not callable(method):
            raise MediaStateError("PLAYBACK_PROVIDER_UNAVAILABLE", retryable=True)
        return method

    async def _call_provider(self, name: str, *args: Any, **kwargs: Any) -> None:
        method = self._provider_method(name)
        try:
            returned = method(*args, **kwargs)
            if inspect.isawaitable(returned):
                await cast(Awaitable[Any], returned)
        except asyncio.CancelledError:
            raise
        except MediaStateError:
            raise
        except Exception as exc:
            raise MediaStateError("PROVIDER_ERROR", retryable=True) from exc

    async def _execute(self, operation: _Operation) -> bool:
        command = operation.command
        if command.action == Action.PLAY:
            return await self._play(operation)
        if command.action == Action.PAUSE:
            return await self._pause(operation)
        if command.action == Action.STOP:
            return await self._stop(operation)
        if command.action == Action.SEEK:
            return await self._seek(operation)
        if command.action == Action.SWITCH_TRACK:
            return await self._switch_track(operation)
        if command.action == Action.SWITCH_STATE:
            return await self._switch_state(operation)
        if command.action == Action.RECOVER:
            return await self._recover(operation)
        raise MediaStateError("INVALID_COMMAND")

    async def _play(self, operation: _Operation) -> bool:
        command = operation.command
        target_id = command.track_id or self._snapshot.track_id
        if target_id is None:
            raise MediaStateError("TRACK_REQUIRED")
        track = self._track(target_id)
        if self._snapshot.playback == PlaybackStatus.PLAYING and self._snapshot.track_id == target_id:
            return False
        audio, audio_degraded = self._resolve_audio(track)
        visual, visual_degraded = self._resolve_visual(
            track,
            self._snapshot.scene,
            required=False,
        )
        position = (
            self._snapshot.position_seconds
            if self._snapshot.track_id == target_id
            and self._snapshot.playback == PlaybackStatus.PAUSED
            else 0.0
        )
        self._check_cancel(operation)
        await self._call_provider(
            "set_source",
            audio,
            visual,
            position_seconds=position,
            playing=True,
        )
        self._check_cancel(operation)
        self._transition(
            playback=PlaybackStatus.PLAYING,
            track_id=target_id,
            position_seconds=position,
            asset_status=(
                AssetStatus.DEGRADED
                if audio_degraded or visual_degraded
                else AssetStatus.AVAILABLE
            ),
            last_error_code=None,
        )
        return True

    async def _pause(self, operation: _Operation) -> bool:
        if self._snapshot.playback != PlaybackStatus.PLAYING:
            return False
        self._check_cancel(operation)
        await self._call_provider("pause")
        self._check_cancel(operation)
        self._transition(playback=PlaybackStatus.PAUSED, last_error_code=None)
        return True

    async def _stop(self, operation: _Operation) -> bool:
        if self._snapshot.playback == PlaybackStatus.STOPPED:
            return False
        self._check_cancel(operation)
        await self._call_provider("stop")
        self._check_cancel(operation)
        self._transition(
            playback=PlaybackStatus.STOPPED,
            position_seconds=0.0,
            last_error_code=None,
        )
        return True

    async def _seek(self, operation: _Operation) -> bool:
        position = operation.command.position_seconds
        if position is None:
            raise MediaStateError("INVALID_POSITION")
        try:
            position = float(position)
        except (TypeError, ValueError) as exc:
            raise MediaStateError("INVALID_POSITION") from exc
        if not math.isfinite(position) or position < 0:
            raise MediaStateError("INVALID_POSITION")
        if self._snapshot.track_id is None:
            raise MediaStateError("TRACK_REQUIRED")
        track = self._track(self._snapshot.track_id)
        if track.duration_seconds is not None and position > track.duration_seconds:
            raise MediaStateError("SEEK_OUT_OF_RANGE")
        if position == self._snapshot.position_seconds:
            return False
        if self._snapshot.playback in {PlaybackStatus.PLAYING, PlaybackStatus.PAUSED}:
            self._check_cancel(operation)
            await self._call_provider("seek", position)
            self._check_cancel(operation)
        self._transition(position_seconds=position, last_error_code=None)
        return True

    async def _switch_track(self, operation: _Operation) -> bool:
        target_id = operation.command.track_id
        track = self._track(target_id)
        if target_id == self._snapshot.track_id:
            return False
        audio, audio_degraded = self._resolve_audio(track)
        visual, visual_degraded = self._resolve_visual(
            track,
            self._snapshot.scene,
            required=False,
        )
        current_playback = self._snapshot.playback
        if current_playback in {PlaybackStatus.PLAYING, PlaybackStatus.PAUSED}:
            self._check_cancel(operation)
            await self._call_provider(
                "set_source",
                audio,
                visual,
                position_seconds=0.0,
                playing=current_playback == PlaybackStatus.PLAYING,
            )
            self._check_cancel(operation)
        self._transition(
            track_id=target_id,
            position_seconds=0.0,
            asset_status=(
                AssetStatus.DEGRADED
                if audio_degraded or visual_degraded
                else AssetStatus.AVAILABLE
            ),
            last_error_code=None,
        )
        return True

    async def _switch_state(self, operation: _Operation) -> bool:
        scene = self._scene_for_command(operation.command)
        if scene == self._snapshot.scene:
            return False
        track = self._track(self._snapshot.track_id) if self._snapshot.track_id else None
        visual: ResolvedAsset | None = None
        degraded = False
        if track is not None:
            visual, degraded = self._resolve_visual(track, scene, required=True)
            if self._snapshot.playback in {PlaybackStatus.PLAYING, PlaybackStatus.PAUSED}:
                if visual is not None:
                    self._check_cancel(operation)
                    await self._call_provider("set_visual", visual)
                    self._check_cancel(operation)
        self._transition(
            time_of_day=scene.time_of_day,
            performance=scene.performance,
            asset_status=(
                AssetStatus.UNKNOWN
                if track is None
                else (AssetStatus.DEGRADED if degraded else AssetStatus.AVAILABLE)
            ),
            last_error_code=None,
        )
        return True

    async def _recover(self, operation: _Operation) -> bool:
        if self._snapshot.playback == PlaybackStatus.STOPPED and self._snapshot.last_error_code is None:
            return False
        self._check_cancel(operation)
        await self._call_provider("stop")
        self._check_cancel(operation)
        self._transition(
            playback=PlaybackStatus.STOPPED,
            position_seconds=0.0,
            asset_status=(
                AssetStatus.AVAILABLE
                if self._snapshot.track_id is not None
                else AssetStatus.UNKNOWN
            ),
            last_error_code=None,
        )
        return True
