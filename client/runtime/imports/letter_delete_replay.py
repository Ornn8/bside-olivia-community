"""Rebuild relationship state without the evidence of a deleted letter.

The live projection (``SQLitePrivateWorldLedger._project``) does not reduce
events in the order they were written. It replays every stored input that
follows the last checkpoint sorted by event time, de-duplicates equivalent
evidence against what was applied earlier in that order, and only then compares
the result with the previous snapshot. Letters are committed late, retried and
imported with their original time, so event time is routinely out of write
order, and the same user sentence repeats across days.

This module repeats those rules over the stored inputs, so the state it
returns is the one the live projection would have produced had the removed
events never been written. Before a rebuild is used, the same code must first
reproduce the stored state at every checkpoint and at the end of the history
without any removal; otherwise the history is refused rather than guessed.
"""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import logging

REPLAY_UNSUPPORTED = 'LETTER_DELETE_RELATIONSHIP_REPLAY_UNSUPPORTED'
LEGACY_UNSUPPORTED = 'LETTER_DELETE_LEGACY_RELATIONSHIP_UNSUPPORTED'
TOMBSTONE = 'letter_source_removed'
_INITIALIZED = 'initialize_historical_relationship'
_CHOICES = ('relationship_route', 'relationship_route_ask')
_SCORES = ('familiarity', 'trust', 'comfort', 'closeness')
_STEP_LIMIT = 300_000  # a history this tangled would also stall the live projection
_log = logging.getLogger(__name__)


def refusal(code, why, cause=None):
    """A user-facing refusal; the reason is logged without any stored content."""
    _log.warning('letter deletion refused: %s (%s%s)', code, why, ': ' + type(cause).__name__ if cause else '')
    return ValueError(code)


def _coded(error):
    """Keep our own refusals; any other failure while replaying is a refusal too."""
    if isinstance(error, ValueError) and str(error) in {REPLAY_UNSUPPORTED, LEGACY_UNSUPPORTED}:
        return error
    return refusal(REPLAY_UNSUPPORTED, 'stored history cannot be replayed', error)


def _time(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00'))


def _content(snapshot):
    return replace(snapshot, version=1)


def _same(left, right):
    return _content(left) == _content(right)


def _floored(projected, current):
    """The live projection never lowers a reached volume (``_project`` tail)."""
    if projected.relationship_volume >= current.relationship_volume:
        return projected
    floors = {'familiarity': 1} if current.relationship_volume >= 2 else {}
    if current.relationship_volume >= 3:
        floors.update(familiarity=35, comfort=35)
    if current.relationship_volume >= 4:
        floors['trust'] = 35
    return replace(projected, relationship_volume=current.relationship_volume,
        volume_started_days=min(current.volume_started_days, len(projected.relationship_days)),
        **{key: max(value, getattr(projected, key)) for key, value in floors.items()})


def outcome_fields(state, applied, reason):
    """Derived eligibility stored beside a replayed input (``result_json``)."""
    from runtime.personal_chat.initiative_profile import profile_from_snapshot
    profile = profile_from_snapshot(state)
    return {'applied': applied, 'reason_code': reason,
            'contact_qualification': sum(getattr(state, key) >= 70 for key in _SCORES) >= 3,
            'initiative_tier': profile.tier, 'initiative_caution': profile.caution}


@dataclass(frozen=True)
class _Item:
    event_id: str
    key: tuple
    kind: str
    candidate: object
    denied: bool


@dataclass(frozen=True)
class _Step:
    state: object
    live_stage: bool
    applied: bool
    reason: str
    skipped: bool = False


class _Trace:
    """One projection journal: stored inputs after a checkpoint, in event-time order.

    Inserting an input recomputes only the steps from its sorted position on,
    which is what the live projection does for a late arrival; an in-order
    arrival takes a single step.
    """

    def __init__(self, base, legacy, watermark, budget):
        from runtime.private_world.reducer import ReducerEventKind
        self.budget = budget
        self.base = base
        self.legacy = legacy
        self.watermark = watermark
        self.interaction = {ReducerEventKind.SUPPORT_RECEIVED, ReducerEventKind.BOUNDARY_RESPECTED,
                            ReducerEventKind.MEANINGFUL_EXCHANGE, ReducerEventKind.SHARED_EXPERIENCE,
                            ReducerEventKind.CONFLICT, ReducerEventKind.REPAIR,
                            ReducerEventKind.RELATIONSHIP_EVIDENCE}
        self.stage_kind = ReducerEventKind.STAGE_CONFIRMED
        self.keys = []
        self.items = []
        self.steps = []
        self.added = []
        self.applied = {}

    @property
    def state(self):
        return self.steps[-1].state if self.steps else self.base

    def insert(self, item):
        position = bisect_left(self.keys, item.key)
        later = self.items[position:]
        for index in range(len(self.items) - 1, position - 1, -1):
            if self.added[index] is not None:
                self.applied[self.added[index]].pop()
        del self.keys[position:], self.items[position:], self.steps[position:], self.added[position:]
        for entry in (item, *later):
            self._push(entry)

    def _predecessor(self, candidate):
        from runtime.private_world.reducer import _DEDUPLICATION_WINDOW
        at = candidate.occurred_at
        found = None
        window = False
        for key in {candidate.semantic_key, 'canonical-interaction:' + candidate.kind.value}:
            times = self.legacy.get(key)
            if times:
                index = bisect_right(times, at)
                if index and (found is None or times[index - 1] > found):
                    found = times[index - 1]
                window = window or index < len(times) and times[index] < at + _DEDUPLICATION_WINDOW
            entries = self.applied.get(key)
            if entries and (found is None or entries[-1][1] > found):
                found = entries[-1][1]
        return at if window else found

    def _push(self, item):
        self.budget[0] -= 1
        if self.budget[0] < 0:
            raise refusal(REPLAY_UNSUPPORTED, 'history is too tangled to replay in time')
        from runtime.private_world.commands import ApplyHistoricalRelationshipEvidence, ConfirmRelationshipStage
        from runtime.private_world.reducer import reduce_private_world, reduce_private_world_command
        previous = self.steps[-1] if self.steps else None
        state = previous.state if previous else self.base
        live = previous.live_stage if previous else self.base.relationship_stage != 'unknown'
        key = None
        if item.kind == 'event':
            candidate = item.candidate
            if candidate.kind in self.interaction:
                candidate = replace(candidate, last_equivalent_at=self._predecessor(candidate))
            if candidate.kind is self.stage_kind and self.watermark and candidate.occurred_at < self.watermark:
                candidate = replace(candidate, target_stage=state.relationship_stage)
            result = reduce_private_world(state, candidate)
            if result.delta.applied:
                key = candidate.semantic_key
                self.applied.setdefault(key, []).append((len(self.items), candidate.occurred_at))
            live = live or candidate.kind is self.stage_kind
            step = _Step(result.snapshot, live, result.delta.applied, result.delta.reason_code)
        elif item.denied:
            # A command refused at submission cannot gain permission later.
            step = _Step(state, live, False, 'INTIMACY_EXCEEDS_STAGE', skipped=True)
        else:
            candidate = item.candidate
            if isinstance(candidate, ApplyHistoricalRelationshipEvidence) and live:
                candidate = replace(candidate, relationship_stage=None)
            result = reduce_private_world_command(state, candidate)
            live = live or isinstance(candidate, ConfirmRelationshipStage)
            step = _Step(result.snapshot, live, result.delta.applied, result.delta.reason_code)
        self.keys.append(item.key)
        self.items.append(item)
        self.steps.append(step)
        self.added.append(key)


@dataclass
class Run:
    rows: dict      # event_id -> rebuilt snapshot, for every event that stores one
    steps: dict     # event_id -> (state after, applied, reason, skipped), for replayed inputs
    final: object


class Replay:
    def __init__(self, ledger, events, inputs, snapshots):
        self.ledger = ledger
        self.events = events
        self.inputs = inputs
        self._audit = {}
        self._items = {}
        try:
            self.stored = {event['event_id']: ledger._strict_stored_snapshot(
                snapshots[event['event_id']]['version'], snapshots[event['event_id']]['payload_json'])
                for event in events if event['event_id'] in snapshots}
        except Exception as error:
            raise refusal(REPLAY_UNSUPPORTED, 'stored snapshot is not canonical', error) from error
        self.version = {event_id: snapshots[event_id]['version'] for event_id in self.stored}

    # -- stored data -------------------------------------------------------

    def audit(self, event):
        value = self._audit.get(event['event_id'])
        if value is None:
            try:
                value = json.loads(event['payload_json'])
            except ValueError as error:
                raise refusal(REPLAY_UNSUPPORTED, 'event audit is not JSON', error) from error
            if not isinstance(value, dict):
                raise refusal(REPLAY_UNSUPPORTED, 'event audit is not an object')
            self._audit[event['event_id']] = value
        return value

    def stored_flag(self, event):
        """Was this event applied, as the live projection later reads it."""
        frozen = self.inputs.get(event['event_id'])
        derived = frozen.get('result_json') if frozen else None
        try:
            assessment = json.loads(derived) if derived else self.audit(event)
        except ValueError as error:
            raise refusal(REPLAY_UNSUPPORTED, 'derived result is not JSON', error) from error
        return assessment.get('applied') is True

    def item(self, event):
        cached = self._items.get(event['event_id'])
        if cached is not None:
            return cached
        from runtime.private_world.commands import command_from_record
        from runtime.private_world.reducer import reducer_event_from_record
        try:
            frozen = json.loads(self.inputs[event['event_id']]['payload_json'])
            if not isinstance(frozen, dict) or set(frozen) != {'schema', 'kind', 'value'} or frozen['schema'] != 1:
                raise ValueError('stored reducer input schema is invalid')
            if frozen['kind'] == 'event':
                candidate = reducer_event_from_record(frozen['value'])
                tie, denied = event['event_id'], False
            elif frozen['kind'] == 'command':
                candidate = command_from_record(frozen['value'])
                tie = frozen['value']['command_id']
                denied = self.audit(event).get('reason_code') == 'INTIMACY_EXCEEDS_STAGE'
            else:
                raise ValueError('stored reducer input kind is invalid')
            if candidate.kind.value != event['event_type'] or candidate.occurred_at != _time(event['occurred_at']):
                raise ValueError('stored reducer input does not match its event')
            result = _Item(event['event_id'], (_time(event['occurred_at']).astimezone(timezone.utc), tie),
                           frozen['kind'], candidate, denied)
        except Exception as error:
            raise _coded(error) from error
        self._items[event['event_id']] = result
        return result

    # -- checkpoints -------------------------------------------------------

    def is_tombstone(self, event):
        return event['event_type'] == TOMBSTONE

    def is_checkpoint(self, event):
        """A kept event whose stored state later projections start from."""
        if self.is_tombstone(event) or event['event_id'] not in self.stored:
            return False
        if event['event_id'] not in self.inputs:
            return True
        return event['event_type'] == _INITIALIZED and self.audit(event).get('reason_code') == 'INITIALIZE_HISTORICAL_RELATIONSHIP'

    def check_removed_opaque(self, removed):
        """A removed event with unexplained stored state cannot be inverted."""
        previous = _empty()
        for event in self.events:
            stored = self.stored.get(event['event_id'])
            if stored is None:
                continue
            if (event['event_id'] in removed and event['event_id'] not in self.inputs and not self.is_tombstone(event)
                    and not _same(stored, previous)):
                raise refusal(LEGACY_UNSUPPORTED, 'removed event changed state without replayable input')
            previous = stored

    def _reapply(self, event, state, before, row):
        """Re-derive an explicit relationship-page choice on a rebuilt state."""
        payload = self.audit(event)
        try:
            from runtime.relationship_growth import volume_updates
            from runtime.relationship_lite import route_changes
            if event['event_type'] == 'relationship_route':
                def derive(snapshot):
                    return replace(snapshot, **route_changes(snapshot, payload['route']))
            else:
                def derive(snapshot):
                    return replace(snapshot, route_asked=True, **volume_updates(snapshot, {}, len(snapshot.relationship_days)))
            # The choice is replayed only if the same rule first reproduces the
            # stored result from the stored previous state.
            if not _same(derive(before), row):
                raise ValueError('stored choice is not reproduced')
            return derive(state)
        except Exception as error:
            raise refusal(LEGACY_UNSUPPORTED, 'relationship choice cannot be replayed', error) from error

    # -- replay ------------------------------------------------------------

    def _legacy(self, end, removed, computed):
        times = {}
        watermark = None
        for event in self.events[:end]:
            if event['event_id'] in removed or self.is_tombstone(event):
                continue
            audit = self.audit(event)
            applied = computed[event['event_id']] if event['event_id'] in computed else self.stored_flag(event)
            when = _time(event['occurred_at'])
            if applied and audit.get('semantic_key'):
                times.setdefault(audit['semantic_key'], []).append(when)
            if 'relationship_stage' in audit.get('change_fields', ()):
                watermark = max(watermark, when) if watermark else when
        for values in times.values():
            values.sort()
        return times, watermark

    def run(self, removed, start, verify=False):
        """Replay events[start:] from the stored checkpoint before them.

        With `verify`, every point where a stored checkpoint closes a journal,
        and the end of the history, must already hold the state that was
        stored there: the replay has to reproduce the live projection first.
        """
        events = self.events
        state = self.stored[events[start - 1]['event_id']] if start else _empty()
        before = state
        computed = {}
        rows = {}
        steps = {}
        budget = [_STEP_LIMIT]
        trace = _Trace(state, *self._legacy(start, removed, computed), budget)

        def close():
            for item, step in zip(trace.items, trace.steps):
                steps[item.event_id] = (step.state, step.applied, step.reason, step.skipped)
                computed[item.event_id] = step.applied

        for index in range(start, len(events)):
            event = events[index]
            event_id = event['event_id']
            row = self.stored.get(event_id)
            if event_id in removed or self.is_tombstone(event):
                pass
            elif self.is_checkpoint(event):
                if verify and not _same(state, before):
                    raise refusal(REPLAY_UNSUPPORTED, 'checkpoint state is not reproduced')
                if _same(state, before):
                    state = row  # the removal did not reach this checkpoint
                elif event['event_type'] in _CHOICES:
                    state = replace(self._reapply(event, state, before, row), version=row.version)
                elif not _same(row, before):
                    raise refusal(LEGACY_UNSUPPORTED, 'checkpoint after the removal cannot be inverted')
                close()
                trace = _Trace(state, *self._legacy(index + 1, removed, computed), budget)
            elif event_id in self.inputs:
                trace.insert(self.item(event))
                state = _floored(trace.state, state)
            if row is not None:
                rows[event_id] = state
                before = row
        close()
        # The newest stored snapshot carries the final state, even when the
        # rebuilt history changes an event that stores no snapshot of its own.
        if self.stored:
            newest = max(self.stored, key=lambda name: self.version[name])
            if verify and not _same(state, self.stored[newest]):
                raise refusal(REPLAY_UNSUPPORTED, 'final state is not reproduced')
            if newest not in rows or not _same(rows[newest], state):
                rows[newest] = state
        return Run(rows, steps, state)

    def rebuild(self, removed):
        """Return the effect of removing `removed`, or None if no input is removed."""
        gone = [index for index, event in enumerate(self.events)
                if event['event_id'] in removed and not self.is_tombstone(event)]
        if not any(self.events[index]['event_id'] in self.inputs for index in gone):
            return None
        start = 0
        for index in range(gone[0] - 1, -1, -1):
            if self.is_checkpoint(self.events[index]):
                start = index + 1
                break
        try:
            original = self.run(frozenset(), start, verify=True)
            for event in self.events[start:]:
                known = original.steps.get(event['event_id'])
                frozen = self.inputs.get(event['event_id'])
                if known is not None and frozen.get('result_json') and known[1] is not self.stored_flag(event):
                    raise refusal(REPLAY_UNSUPPORTED, 'applied evidence is not reproduced')
            result = self.run(removed, start)
        except Exception as error:
            raise _coded(error) from error
        # Only what the removal itself changes is rewritten.
        rows = {event_id: state for event_id, state in result.rows.items()
                if not _same(state, original.rows.get(event_id, self.stored[event_id]))}
        outcomes = {}
        for event_id, step in result.steps.items():
            known = original.steps[event_id]
            if not step[3] and ((step[1], step[2]) != (known[1], known[2]) or not _same(step[0], known[0])):
                outcomes[event_id] = outcome_fields(step[0], step[1], step[2])
        return Rebuilt(rows, outcomes, result.final)


@dataclass
class Rebuilt:
    rows: dict        # event_id -> snapshot whose stored state changes
    outcomes: dict    # event_id -> replacement derived fields for a surviving input
    final: object


def _empty():
    from runtime.private_world.port import PrivateWorldSnapshot
    return PrivateWorldSnapshot()
