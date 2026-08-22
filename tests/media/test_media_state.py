"""B09 media-state coverage using local synthetic fixtures only."""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

import pytest

from media_state import (
    AssetKind,
    FallbackPolicy,
    ManifestAssetResolver,
    MediaCommand,
    MediaStateError,
    MediaStateMachine,
    MusicCatalog,
    OperationStatus,
    PerformanceMode,
    PlaybackStatus,
    TimeOfDay,
    TrackDefinition,
    contract_document,
)
from tools import asset_manifest


ROOT = Path(__file__).resolve().parents[2]


def _assert_evidence_tmp(path: Path) -> None:
    evidence = (ROOT / ".evidence").resolve()
    assert path.resolve().is_relative_to(evidence)


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 80)


def _fixture(tmp_path: Path) -> tuple[ManifestAssetResolver, MusicCatalog, dict[str, str], Path]:
    _assert_evidence_tmp(tmp_path)
    root = tmp_path / "synthetic-original"
    _write_wav(root / "track-a.wav")
    _write_wav(root / "track-b.wav")
    # These are synthetic placeholders for the manifest boundary only; no test
    # treats their bytes as playable visual evidence.
    for name in ("day-idle.mp4", "dusk-piano.mp4", "fallback.mp4"):
        (root / name).write_bytes(b"synthetic video fixture")
    manifest = asset_manifest.scan_roots({"original": root})
    for item in manifest["items"]:
        if item["category"] == "video":
            item["probe_status"] = "unavailable"
            item["reason"] = "probe_tool_unavailable"
    resolver = ManifestAssetResolver(manifest, {"original": root})
    references: dict[str, str] = {}
    for item in manifest["items"]:
        references[item["relative_path"]] = item["logical_id"]
    catalog = MusicCatalog(
        [
            TrackDefinition(
                track_id="fixture-track",
                audio_asset_ref=references["track-a.wav"],
                visual_asset_refs={
                    "day/idle": references["day-idle.mp4"],
                    "dusk/piano_performance": references["dusk-piano.mp4"],
                },
                fallback_visual_asset_refs={
                    "night/idle": references["fallback.mp4"],
                },
                duration_seconds=30,
            ),
            TrackDefinition(
                track_id="second-track",
                audio_asset_ref=references["track-b.wav"],
                visual_asset_refs={"day/idle": references["day-idle.mp4"]},
                duration_seconds=30,
            ),
        ]
    )
    return resolver, catalog, references, root


class RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.block_source = False
        self.fail_source = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def set_source(self, audio, visual, *, position_seconds: float, playing: bool) -> None:
        del audio, visual, position_seconds, playing
        self.calls.append("set_source")
        if self.fail_source:
            self.fail_source = False
            raise RuntimeError("synthetic provider failure")
        if self.block_source:
            self.started.set()
            await self.release.wait()

    async def pause(self) -> None:
        self.calls.append("pause")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def seek(self, position_seconds: float) -> None:
        del position_seconds
        self.calls.append("seek")

    async def set_visual(self, visual) -> None:
        del visual
        self.calls.append("set_visual")


def test_manifest_resolver_rechecks_category_missing_file_and_hash(tmp_path: Path) -> None:
    resolver, _, references, root = _fixture(tmp_path)
    audio_ref = references["track-a.wav"]

    resolved = resolver.resolve(audio_ref, AssetKind.AUDIO)
    assert resolved.kind == AssetKind.AUDIO
    assert resolved.path == (root / "track-a.wav").resolve()

    with pytest.raises(MediaStateError) as category_error:
        resolver.resolve(audio_ref, AssetKind.VIDEO)
    assert category_error.value.code == "ASSET_CATEGORY_MISMATCH"

    (root / "track-a.wav").unlink()
    with pytest.raises(MediaStateError) as missing_error:
        resolver.resolve(audio_ref, AssetKind.AUDIO)
    assert missing_error.value.code == "ASSET_MISSING"

    _write_wav(root / "track-a.wav")
    (root / "track-a.wav").write_bytes(b"tampered")
    with pytest.raises(MediaStateError) as hash_error:
        resolver.resolve(audio_ref, AssetKind.AUDIO)
    assert hash_error.value.code == "ASSET_HASH_MISMATCH"


def test_normal_play_pause_seek_stop_and_idempotency_are_deterministic(tmp_path: Path) -> None:
    resolver, catalog, _, _ = _fixture(tmp_path)
    provider = RecordingProvider()

    async def scenario() -> None:
        machine = MediaStateMachine(catalog, resolver, provider)
        first = machine.submit(MediaCommand.play("fixture-track"), request_id="play-1")
        assert (await first.wait()).status == OperationStatus.COMPLETED
        assert machine.snapshot().playback == PlaybackStatus.PLAYING

        duplicate = machine.submit(MediaCommand.play("fixture-track"), request_id="play-1")
        assert duplicate.operation_id == first.operation_id
        assert (await duplicate.wait()).status == OperationStatus.COMPLETED
        noop = await machine.submit(MediaCommand.play("fixture-track")).wait()
        assert noop.status == OperationStatus.NOOP

        paused = await machine.submit(MediaCommand.pause()).wait()
        assert paused.status == OperationStatus.COMPLETED
        assert machine.snapshot().playback == PlaybackStatus.PAUSED
        seeked = await machine.submit(MediaCommand.seek(12)).wait()
        assert seeked.status == OperationStatus.COMPLETED
        assert machine.snapshot().position_seconds == 12
        stopped = await machine.submit(MediaCommand.stop()).wait()
        assert stopped.status == OperationStatus.COMPLETED
        assert machine.snapshot().playback == PlaybackStatus.STOPPED
        assert machine.snapshot().position_seconds == 0
        assert (await machine.submit(MediaCommand.stop()).wait()).status == OperationStatus.NOOP
        assert provider.calls == ["set_source", "pause", "seek", "stop"]

        out_of_range = await machine.submit(MediaCommand.seek(31)).wait()
        assert out_of_range.status == OperationStatus.FAILED
        assert out_of_range.error_code == "SEEK_OUT_OF_RANGE"
        assert machine.snapshot().playback == PlaybackStatus.STOPPED

    asyncio.run(scenario())


def test_state_switch_requires_original_visual_or_uses_declared_fallback(tmp_path: Path) -> None:
    resolver, catalog, _, _ = _fixture(tmp_path)

    async def scenario() -> None:
        provider = RecordingProvider()
        machine = MediaStateMachine(catalog, resolver, provider)
        await machine.submit(MediaCommand.play("fixture-track")).wait()
        changed = await machine.submit(
            MediaCommand.switch_state(
                time_of_day=TimeOfDay.DUSK,
                performance=PerformanceMode.PIANO_PERFORMANCE,
            )
        ).wait()
        assert changed.status == OperationStatus.COMPLETED
        assert machine.snapshot().time_of_day == TimeOfDay.DUSK
        assert machine.snapshot().performance == PerformanceMode.PIANO_PERFORMANCE
        assert "set_visual" in provider.calls

        missing = await machine.submit(
            MediaCommand.switch_state(time_of_day=TimeOfDay.NIGHT)
        ).wait()
        assert missing.status == OperationStatus.FAILED
        assert missing.error_code == "ASSET_NOT_FOUND"
        assert machine.snapshot().time_of_day == TimeOfDay.DUSK

        fallback_provider = RecordingProvider()
        fallback = MediaStateMachine(
            catalog,
            resolver,
            fallback_provider,
            fallback_policy=FallbackPolicy.USE_DECLARED_FALLBACK,
        )
        await fallback.submit(MediaCommand.play("fixture-track")).wait()
        degraded = await fallback.submit(
            MediaCommand.switch_state(time_of_day=TimeOfDay.NIGHT)
        ).wait()
        assert degraded.status == OperationStatus.COMPLETED
        assert degraded.snapshot.asset_status.value == "degraded"
        assert degraded.snapshot.time_of_day == TimeOfDay.NIGHT

    asyncio.run(scenario())


def test_duplicate_conflict_concurrency_cancel_and_recovery(tmp_path: Path) -> None:
    resolver, catalog, _, _ = _fixture(tmp_path)

    async def scenario() -> None:
        provider = RecordingProvider()
        machine = MediaStateMachine(catalog, resolver, provider)
        first = machine.submit(MediaCommand.play("fixture-track"), request_id="same")
        assert (await first.wait()).status == OperationStatus.COMPLETED
        with pytest.raises(MediaStateError) as conflict:
            machine.submit(MediaCommand.stop(), request_id="same")
        assert conflict.value.code == "REQUEST_ID_REUSED"

        cancel_provider = RecordingProvider()
        cancel_provider.block_source = True
        cancel_machine = MediaStateMachine(catalog, resolver, cancel_provider)
        pending = cancel_machine.submit(MediaCommand.play("fixture-track"))
        await cancel_provider.started.wait()
        canceled = await cancel_machine.cancel(pending.operation_id)
        assert canceled.status == OperationStatus.CANCELED
        assert cancel_machine.snapshot().playback == PlaybackStatus.STOPPED
        assert cancel_machine.snapshot().track_id is None

        cancel_provider.block_source = False
        retry = cancel_machine.retry(pending.operation_id)
        assert (await retry.wait()).status == OperationStatus.COMPLETED

        provider.fail_source = True
        failed = await machine.submit(MediaCommand.play("second-track")).wait()
        assert failed.status == OperationStatus.FAILED
        assert failed.error_code == "PROVIDER_ERROR"
        assert machine.snapshot().playback == PlaybackStatus.ERROR
        recovered = await machine.submit(MediaCommand.recover()).wait()
        assert recovered.status == OperationStatus.COMPLETED
        assert machine.snapshot().playback == PlaybackStatus.STOPPED
        assert machine.snapshot().last_error_code is None

    asyncio.run(scenario())


def test_concurrent_commands_are_serialized_in_submission_order(tmp_path: Path) -> None:
    resolver, catalog, _, _ = _fixture(tmp_path)
    provider = RecordingProvider()

    async def scenario() -> None:
        machine = MediaStateMachine(catalog, resolver, provider)
        play = machine.submit(MediaCommand.play("fixture-track"))
        pause = machine.submit(MediaCommand.pause())
        first, second = await asyncio.gather(play.wait(), pause.wait())
        assert first.status == OperationStatus.COMPLETED
        assert second.status == OperationStatus.COMPLETED
        assert machine.snapshot().playback == PlaybackStatus.PAUSED
        assert provider.calls == ["set_source", "pause"]

    asyncio.run(scenario())


def test_cancel_waiting_lock_cannot_restore_stale_snapshot_after_prior_commit(tmp_path: Path) -> None:
    resolver, catalog, _, _ = _fixture(tmp_path)
    provider = RecordingProvider()

    async def scenario() -> None:
        machine = MediaStateMachine(catalog, resolver, provider)
        provider.block_source = True
        first = machine.submit(MediaCommand.play("fixture-track"))
        await provider.started.wait()
        second = machine.submit(MediaCommand.switch_track("second-track"))

        canceled = await machine.cancel(second.operation_id)
        assert canceled.status == OperationStatus.CANCELED
        provider.block_source = False
        provider.release.set()
        completed = await first.wait()

        assert completed.status == OperationStatus.COMPLETED
        assert machine.snapshot().playback == PlaybackStatus.PLAYING
        assert machine.snapshot().track_id == "fixture-track"

    asyncio.run(scenario())


def test_missing_provider_is_explicit_and_events_are_privacy_safe(tmp_path: Path) -> None:
    resolver, catalog, references, root = _fixture(tmp_path)
    events = []

    async def scenario() -> None:
        machine = MediaStateMachine(catalog, resolver, None, event_sink=events.append)
        result = await machine.submit(MediaCommand.play("fixture-track")).wait()
        assert result.status == OperationStatus.FAILED
        assert result.error_code == "PLAYBACK_PROVIDER_UNAVAILABLE"
        assert machine.snapshot().playback == PlaybackStatus.ERROR

    asyncio.run(scenario())
    encoded = json.dumps([event.to_dict() for event in events], ensure_ascii=False)
    assert str(root) not in encoded
    assert references["track-a.wav"] not in encoded
    assert "path" not in encoded
    assert "asset_ref" not in encoded
    assert all("original" not in key for event in events for key in event.to_dict())


def test_committed_contract_document_and_example_are_path_free() -> None:
    document = contract_document()
    example = json.loads(
        (ROOT / "contracts" / "media_state.example.json").read_text(encoding="utf-8")
    )
    schema = json.loads(
        (ROOT / "contracts" / "media_state.schema.json").read_text(encoding="utf-8")
    )
    assert document == example
    assert schema["properties"]["contract_version"]["const"] == "b09.v1"
    serialized = json.dumps(example, ensure_ascii=False)
    assert "logical_id" not in serialized
    assert "relative_path" not in serialized
    assert example["privacy"]["source_paths_in_events"] is False
