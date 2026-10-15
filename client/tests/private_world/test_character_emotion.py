"""Phase 3's source-bound emotion contract, using synthetic local evidence.

Contract: .evidence/phase3-emotion-contract-20260927.md.
These checks cover storage/projection, not evaluator accuracy or product wiring.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import pytest

from runtime.private_world.character_emotion import CharacterEmotionStore, REACTION_WINDOW
from runtime.private_world.daily_life import DailyLifeStore


NOW = datetime(2026, 9, 27, 2, tzinfo=timezone.utc)


@pytest.fixture
def stores(tmp_path):
    path = tmp_path / "daily_life.sqlite3"
    return DailyLifeStore(path), CharacterEmotionStore(path)


def appraisal(source_id, quote, reaction="none", *, concern=None, revises=None,
              reported_affect=None, action_tendency="none", goal_or_need=None):
    return dict(source_id=source_id, quote=quote, reaction=reaction,
                goal_or_need=goal_or_need, action_tendency=action_tendency,
                concern=concern, revises=revises, reported_affect=reported_affect)


def commit(store, *items):
    basis = store.assessment([item["source_id"] for item in items])
    assert store.commit(basis, {"appraisals": list(items)}) is True
    return basis


def source_ids(view):
    return [item["source_id"] for item in view["reactions"]]


@pytest.mark.parametrize('second', ['open', 'resolve'])
def test_multiple_concerns_reject_duplicate_ids_atomically(stores, second):
    _, emotion = stores
    emotion.receive('new', '两个想法。', occurred_at=NOW)
    changes = [dict(id='emotion:new', action='open', summary='第一个。'),
               dict(id='emotion:new', action=second, summary='第二个。')]
    with pytest.raises(ValueError, match='EMOTION_APPRAISAL_INVALID'):
        emotion.commit(emotion.assessment(['new']), {'appraisals': [
            appraisal('new', '两个想法。', concern=changes)]})
    assert not emotion.view(now=NOW)['concerns']
    assert emotion.pending_source_ids(before=NOW) == ['new']


def test_multiple_concern_resolution_failure_rolls_back_other_valid_changes(stores):
    _, emotion = stores
    emotion.receive('old', '事情没确定。', occurred_at=NOW)
    commit(emotion, appraisal('old', '事情没确定。', concern={
        'id': 'emotion:old', 'action': 'open', 'summary': '事情没确定。'}))
    later = NOW+timedelta(minutes=1)
    emotion.receive('new', '已经解决了，还有一件新事情。', occurred_at=later)
    changes = [dict(id='emotion:old', action='resolve', summary='事情没确定。'),
               dict(id='emotion:missing', action='resolve', summary='未曾打开。'),
               dict(id='emotion:new', action='open', summary='一件新事情。')]
    with pytest.raises(ValueError):
        emotion.commit(emotion.assessment(['new']), {'appraisals': [
            appraisal('new', '已经解决了，还有一件新事情。', concern=changes)]})
    assert [c['id'] for c in emotion.view(now=later)['concerns']] == ['emotion:old']
    assert emotion.pending_source_ids(before=later) == ['new']


def test_received_input_reacts_before_any_reply_exists_and_survives_restart(stores):
    life, emotion = stores
    text = "你可以慢慢来，我没有催你。"
    assert emotion.receive("qq:owner:m1", text, occurred_at=NOW) is True
    basis = emotion.assessment(["qq:owner:m1"])
    source = basis["sources"][0]
    assert source["source_kind"] == "received_input"
    assert source["text"] == text and source["source_hash"]
    assert datetime.fromisoformat(source["occurred_at"]) == NOW
    assert basis["context_version"]
    assert {"concerns", "prior_appraisals"} <= basis.keys()
    emotion.commit(basis, {"appraisals": [appraisal(
        "qq:owner:m1", text, "relieved", action_tendency="continue",
        goal_or_need="可以按自己的节奏完成练习")]})
    expected = emotion.view(now=NOW)
    assert expected["reaction_subject"] == "character"
    assert expected["interpretation_only"] is True
    assert source_ids(expected) == ["qq:owner:m1"]
    assert expected["reactions"][0]["action_tendency"] == "continue"
    assert CharacterEmotionStore(life.path).view(now=NOW) == expected
    with life._db() as db:
        assert db.execute("SELECT count(*) FROM life_moments").fetchone()[0] == 0


def test_only_published_world_can_supply_own_life_source(stores):
    life, emotion = stores
    life.record_exchange("reply:draft:1", "练完了吗？", "我已经练好了。", [], occurred_at=NOW)
    for source_id in ("day:missing", "reply:draft:1"):
        with pytest.raises(ValueError, match="EMOTION_SOURCE_UNAVAILABLE"):
            emotion.publish(source_id)
    note = "在家里练琴：左手两处衔接。"
    life.publish_day("day:practice", dict(location="家里", activity="练琴", note=note), [], occurred_at=NOW)
    before = life.snapshot(NOW)
    assert emotion.publish("day:practice") is True
    assert emotion.publish("day:practice") is False
    basis = emotion.assessment(["day:practice"])
    assert basis["sources"][0]["source_kind"] == "published_world"
    assert note in basis["sources"][0]["text"]
    emotion.commit(basis, {"appraisals": [appraisal("day:practice", note, "none")]})
    assert life.snapshot(NOW) == before


def test_merged_batches_and_no_effect_sources_are_evaluated_once(stores):
    _, emotion = stores
    for key, text in (("m1", "慢慢来"), ("m2", "早上好")):
        emotion.receive(key, text, occurred_at=NOW)
    basis = commit(emotion, appraisal("m1", "慢慢来", "relieved"))
    assert emotion.commit(basis, {"appraisals": [appraisal("m1", "慢慢来", "relieved")]}) is False
    assert emotion.receive("m1", "慢慢来", occurred_at=NOW) is False
    merged = emotion.assessment(["m1", "m2", "m1"])
    assert [s["source_id"] for s in merged["sources"]] == ["m2"]
    emotion.commit(merged, {"appraisals": [appraisal("m2", "")]})
    assert emotion.assessment(["m1", "m2"])["sources"] == []
    assert source_ids(emotion.view(now=NOW)) == ["m1"]
    with pytest.raises(ValueError, match="EMOTION_SOURCE_CONFLICT"):
        emotion.receive("m1", "修改原文", occurred_at=NOW)


@pytest.mark.parametrize("change", [
    {"quote": "并不存在的原文"},
    {"source_id": "unknown-source"},
    {"reaction": "user_is_angry"},
    {"reported_affect": {"subject": "character", "quote": "我很烦", "affect": "frustrated"}},
    {"trust_delta": -20},
])
def test_untrusted_appraisal_is_atomic_and_cannot_claim_other_authority(stores, change):
    _, emotion = stores
    emotion.receive("m1", "我很烦", occurred_at=NOW)
    basis = emotion.assessment(["m1"])
    bad = {**appraisal("m1", "我很烦", "concerned"), **change}
    with pytest.raises(ValueError, match="EMOTION_APPRAISAL_INVALID"):
        emotion.commit(basis, {"appraisals": [bad]})
    assert emotion.view(now=NOW)["reactions"] == []
    assert len(emotion.assessment(["m1"])["sources"]) == 1


def test_source_snapshot_cannot_be_edited_before_commit(stores):
    _, emotion = stores
    emotion.receive("m1", "只是说别人", occurred_at=NOW)
    basis = emotion.assessment(["m1"])
    tampered = deepcopy(basis)
    tampered["sources"][0]["text"] = "我在骂你"
    with pytest.raises(ValueError, match="EMOTION_(CONTEXT_STALE|APPRAISAL_INVALID)"):
        emotion.commit(tampered, {"appraisals": [appraisal("m1", "我在骂你", "hurt")]})
    assert emotion.view(now=NOW)["reactions"] == []


def test_reported_user_feeling_is_not_automatically_her_feeling(stores):
    _, emotion = stores
    text = "我很烦，老板又在催我。"
    emotion.receive("m1", text, occurred_at=NOW)
    commit(emotion, appraisal("m1", "", reported_affect={
        "subject": "user", "quote": "我很烦", "affect": "frustrated"}))
    view = emotion.view(now=NOW)
    assert view["reactions"] == [] and view["concerns"] == []
    assert view["reported_affects"][0]["subject"] == "user"
    assert view["reported_affects"][0]["source_id"] == "m1"


def test_time_settles_reaction_without_claiming_concern_resolved(stores):
    life, emotion = stores
    emotion.receive("m1", "约好的练习时间还没有确定。", occurred_at=NOW)
    commit(emotion, appraisal("m1", "练习时间还没有确定", "concerned", concern={
        "id": "practice-time", "action": "open", "summary": "还在意练习时间没有确定"}))
    later = NOW + REACTION_WINDOW + timedelta(seconds=1)
    assert source_ids(emotion.view(now=NOW)) == ["m1"]
    settled = emotion.view(now=later)
    assert settled["reactions"] == []
    assert [c["id"] for c in settled["concerns"]] == ["practice-time"]
    assert CharacterEmotionStore(life.path).view(now=later) == settled
    with life._db() as db:
        assert db.execute("SELECT count(*) FROM life_moments").fetchone()[0] == 0


def test_related_new_input_does_not_withdraw_prior_interpretation_by_itself(stores):
    _, emotion = stores
    emotion.receive("old", "你必须马上答应。", occurred_at=NOW)
    commit(emotion, appraisal("old", "你必须马上答应", "hurt"))
    emotion.receive("clarification", "刚才是引用别人说的话。", occurred_at=NOW + timedelta(minutes=1))
    assert source_ids(emotion.view(now=NOW + timedelta(minutes=1))) == ["old"]
    # Association alone is not truth authority; a distinct evaluator must decide.
    commit(emotion, appraisal("clarification", "刚才是引用别人说的话", "none"))
    assert source_ids(emotion.view(now=NOW + timedelta(minutes=1))) == ["old"]


def test_explicit_reappraisal_preserves_old_time_view_and_rejects_stale_revision(stores):
    _, emotion = stores
    emotion.receive("old", "你必须马上答应。", occurred_at=NOW)
    commit(emotion, appraisal("old", "你必须马上答应", "hurt", concern={
        "id": "pressure", "action": "open", "summary": "在意对方是否不允许自己拒绝"}))
    for key in ("slow", "current"):
        emotion.receive(key, "刚才是引用别人说的话。", occurred_at=NOW + timedelta(minutes=1))
    slow = emotion.assessment(["slow"])
    current = emotion.assessment(["current"])
    revise = {"source_id": "old", "action": "withdraw"}
    emotion.commit(current, {"appraisals": [appraisal(
        "current", "刚才是引用别人说的话", "relieved", revises=revise)]})
    with pytest.raises(ValueError, match="EMOTION_CONTEXT_STALE"):
        emotion.commit(slow, {"appraisals": [appraisal(
            "slow", "刚才是引用别人说的话", "relieved", revises=revise)]})
    assert source_ids(emotion.view(now=NOW)) == ["old"]
    now = emotion.view(now=NOW + timedelta(minutes=1))
    assert source_ids(now) == ["current"] and now["concerns"] == []


def test_unrelated_input_does_not_invalidate_inflight_or_existing_appraisal(stores):
    _, emotion = stores
    emotion.receive("first", "你可以慢慢来。", occurred_at=NOW)
    first = emotion.assessment(["first"])
    emotion.receive("second", "午饭吃了吗？", occurred_at=NOW + timedelta(minutes=1))
    assert emotion.commit(first, {"appraisals": [appraisal("first", "你可以慢慢来", "relieved")]}) is True
    commit(emotion, appraisal("second", ""))
    assert source_ids(emotion.view(now=NOW + timedelta(minutes=1))) == ["first"]


def test_concern_changed_after_assessment_cannot_be_closed_by_stale_result(stores):
    _, emotion = stores
    emotion.receive("opening", "时间还没有确定。", occurred_at=NOW)
    commit(emotion, appraisal("opening", "时间还没有确定", "concerned", concern={
        "id": "practice-time", "action": "open", "summary": "还在意练习时间未定"}))
    emotion.receive("slow", "我会确认时间。", occurred_at=NOW + timedelta(minutes=1))
    slow = emotion.assessment(["slow"])
    emotion.receive("new-detail", "老师可能还要改时间。", occurred_at=NOW + timedelta(minutes=2))
    commit(emotion, appraisal("new-detail", "老师可能还要改时间", "concerned", concern={
        "id": "practice-time", "action": "open", "summary": "安排仍有新的变动可能"}))
    with pytest.raises(ValueError, match="EMOTION_CONTEXT_STALE"):
        emotion.commit(slow, {"appraisals": [appraisal("slow", "我会确认时间", "relieved", concern={
            "id": "practice-time", "action": "resolve", "summary": "对此暂时放心"})]})
    assert [c["id"] for c in emotion.view(now=NOW + timedelta(minutes=2))["concerns"]] == ["practice-time"]


def test_model_completion_order_converges_after_stale_context_reassessment(tmp_path):
    outputs = []
    for index, order in enumerate((("first", "second"), ("second", "first"))):
        path = tmp_path / f"order-{index}.sqlite3"
        DailyLifeStore(path)
        emotion = CharacterEmotionStore(path)
        emotion.receive("first", "慢慢来", occurred_at=NOW)
        emotion.receive("second", "还有一处没有确定", occurred_at=NOW + timedelta(minutes=1))
        bases = {key: emotion.assessment([key]) for key in order}
        values = {"first": appraisal("first", "慢慢来", "relieved"),
                  "second": appraisal("second", "还有一处没有确定", "concerned")}
        for key in order:
            if index == 0 and key == "second":
                with pytest.raises(ValueError, match="EMOTION_CONTEXT_STALE"):
                    emotion.commit(bases[key], {"appraisals": [values[key]]})
            else:
                emotion.commit(bases[key], {"appraisals": [values[key]]})
        assert emotion.pending_source_ids() == ["second"]
        # The evaluator must see the new causal context before its later result
        # can apply, whether it finished before or after the earlier appraisal.
        commit(emotion, values["second"])
        outputs.append(emotion.view(now=NOW + timedelta(minutes=1)))
    assert outputs[0] == outputs[1]
    assert set(source_ids(outputs[0])) == {"first", "second"}


def test_withdrawal_of_withdrawal_restores_only_the_original_interpretation(stores):
    _, emotion = stores
    emotion.receive("original", "你必须现在答应。", occurred_at=NOW)
    commit(emotion, appraisal("original", "你必须现在答应", "hurt", concern={
        "id": "pressure", "action": "open", "summary": "在意对方是否容许拒绝"}))
    emotion.receive("revision", "我是在引用别人的话。", occurred_at=NOW + timedelta(minutes=1))
    commit(emotion, appraisal("revision", "我是在引用别人的话", "relieved",
                              revises={"source_id": "original", "action": "withdraw"}))
    emotion.receive("revision-again", "刚才的澄清也只是转述。", occurred_at=NOW + timedelta(minutes=2))
    commit(emotion, appraisal("revision-again", "刚才的澄清也只是转述", "concerned",
                              revises={"source_id": "revision", "action": "withdraw"}))
    assert source_ids(emotion.view(now=NOW + timedelta(minutes=1))) == ["revision"]
    restored = emotion.view(now=NOW + timedelta(minutes=2))
    assert source_ids(restored) == ["original", "revision-again"]
    assert [c["id"] for c in restored["concerns"]] == ["pressure"]


def test_withdrawing_one_source_preserves_independent_concern_contributor(stores):
    _, emotion = stores
    for index, key in enumerate(("first", "independent")):
        emotion.receive(key, "练习时间尚未确定。", occurred_at=NOW + timedelta(minutes=index))
        commit(emotion, appraisal(key, "练习时间尚未确定", "concerned", concern={
            "id": "practice", "action": "open", "summary": "仍在意练习时间"}))
    emotion.receive("clarification", "第一条消息是我引用的旧记录。", occurred_at=NOW + timedelta(minutes=2))
    commit(emotion, appraisal("clarification", "第一条消息是我引用的旧记录", "relieved",
                              revises={"source_id": "first", "action": "withdraw"}))
    concern = emotion.view(now=NOW + timedelta(minutes=2))["concerns"][0]
    assert concern["id"] == "practice"
    assert concern["source_ids"] == ["independent"]


@pytest.mark.parametrize("operation", ["open", "resolve", "withdraw"])
def test_late_input_cannot_change_a_target_from_its_future(stores, operation):
    _, emotion = stores
    future = NOW + timedelta(minutes=10)
    emotion.receive("newer", "现在练习安排有变动。", occurred_at=future)
    commit(emotion, appraisal("newer", "现在练习安排有变动", "concerned", concern={
        "id": "practice", "action": "open", "summary": "在意新的安排变动"}))
    emotion.receive("late", "我会确认安排。", occurred_at=NOW)
    item = appraisal("late", "我会确认安排", "relieved")
    if operation == "withdraw":
        item["revises"] = {"source_id": "newer", "action": "withdraw"}
    else:
        item["concern"] = {"id": "practice", "action": operation, "summary": "对安排的另一种理解"}
    with pytest.raises(ValueError, match="EMOTION_CONTEXT_STALE"):
        emotion.commit(emotion.assessment(["late"], now=future), {"appraisals": [item]})
    assert emotion.pending_source_ids(before=future) == ["late"]
    assert source_ids(emotion.view(now=future)) == ["newer"]


def test_pending_recovery_and_assessment_keep_time_cutoff(stores):
    _, emotion = stores
    emotion.receive("past", "慢慢来", occurred_at=NOW)
    emotion.receive("future", "明天再说", occurred_at=NOW + timedelta(days=1))
    assert emotion.pending_source_ids(before=NOW) == ["past"]
    snapshot = emotion.assessment(["past"], now=NOW)
    assert datetime.fromisoformat(snapshot["as_of"]) == NOW
    assert "future" not in json.dumps(snapshot, ensure_ascii=False)
    with pytest.raises(ValueError, match="EMOTION_SOURCE_UNAVAILABLE"):
        emotion.assessment(["future"], now=NOW)
    with pytest.raises(ValueError, match="EMOTION_INPUT_INVALID"):
        emotion.pending_source_ids(limit=0)


def test_size_limits_and_missing_batch_items_reject_without_partial_state(stores):
    from runtime.private_world.character_emotion import MAX_SOURCE_CHARS
    _, emotion = stores
    with pytest.raises(ValueError, match="EMOTION_SOURCE_INVALID"):
        emotion.receive("large", "甲" * (MAX_SOURCE_CHARS + 1), occurred_at=NOW)
    with pytest.raises(ValueError, match="EMOTION_TIME_INVALID"):
        emotion.receive("naive", "早上好", occurred_at=NOW.replace(tzinfo=None))
    emotion.receive("one", "慢慢来", occurred_at=NOW)
    emotion.receive("two", "继续吧", occurred_at=NOW)
    basis = emotion.assessment(["one", "two"])
    for items in ([appraisal("one", "慢慢来", "relieved")],
                  [appraisal("one", "慢慢来", "relieved"), appraisal("two", "继续吧", goal_or_need="甲" * 161)],
                  [appraisal("one", "慢慢来", "relieved"), appraisal("two", "继续吧", concern={
                      "id": "practice", "action": "open", "summary": "甲" * 201})]):
        with pytest.raises(ValueError, match="EMOTION_APPRAISAL_INVALID"):
            emotion.commit(basis, {"appraisals": items})
        assert emotion.view(now=NOW)["reactions"] == []
        assert emotion.pending_source_ids() == ["one", "two"]


def test_persisted_source_hash_is_rechecked_before_evaluation_or_commit(stores):
    life, emotion = stores
    emotion.receive("one", "慢慢来", occurred_at=NOW)
    basis = emotion.assessment(["one"])
    with life._db() as db:
        row = db.execute("SELECT payload FROM character_emotion_sources WHERE source_id='one'").fetchone()
        value = json.loads(row[0])
        value["text"] = "原文被错误改写"
        db.execute("UPDATE character_emotion_sources SET payload=? WHERE source_id='one'", (json.dumps(value),))
    with pytest.raises(ValueError, match="EMOTION_STORED_EVIDENCE_INVALID"):
        emotion.assessment(["one"])
    with pytest.raises(ValueError, match="EMOTION_STORED_EVIDENCE_INVALID"):
        emotion.commit(basis, {"appraisals": [appraisal("one", "慢慢来", "relieved")]})


def test_late_evidence_reopens_existing_concern_and_requeues_stale_resolution(stores):
    life, emotion = stores
    emotion.receive("opening", "练习时间还没有定。", occurred_at=NOW)
    commit(emotion, appraisal("opening", "练习时间还没有定", "concerned", concern={
        "id": "practice", "action": "open", "summary": "在意练习时间未定"}))
    emotion.receive("resolution", "这个时间我已经确认过了。", occurred_at=NOW + timedelta(minutes=2))
    resolution = appraisal("resolution", "这个时间我已经确认过了", "relieved", concern={
        "id": "practice", "action": "resolve", "summary": "对之前的时间安排放心了"})
    commit(emotion, resolution)
    assert emotion.view(now=NOW + timedelta(minutes=3))["concerns"] == []

    emotion.receive("late-detail", "还有老师那边的时间没有确定。", occurred_at=NOW + timedelta(minutes=1))
    basis = emotion.assessment(["late-detail"], now=NOW + timedelta(minutes=3))
    historical = basis["source_contexts"]["late-detail"]["concerns"]
    assert historical[0]["status"] == "open" and historical[0]["source_ids"] == ["opening"]
    emotion.commit(basis, {"appraisals": [appraisal("late-detail", "老师那边的时间没有确定", "concerned", concern={
        "id": "practice", "action": "open", "summary": "还在意老师的安排没有确定"})]})

    unsettled = emotion.view(now=NOW + timedelta(minutes=3))
    assert "resolution" not in source_ids(unsettled)
    assert unsettled["concerns"][0]["source_ids"] == ["opening", "late-detail"]
    assert emotion.pending_source_ids() == ["resolution"]
    restarted = CharacterEmotionStore(life.path)
    assert restarted.pending_source_ids() == ["resolution"]
    assert restarted.view(now=NOW + timedelta(minutes=3)) == unsettled
    retry = restarted.assessment(["resolution"], now=NOW + timedelta(minutes=3))
    assert retry["source_contexts"]["resolution"]["concerns"][0]["source_ids"] == ["opening", "late-detail"]
    assert restarted.commit(retry, {"appraisals": [resolution]}) is True
    assert restarted.view(now=NOW + timedelta(minutes=3))["concerns"] == []
    assert restarted.pending_source_ids() == []
    with life._db() as db:
        assert db.execute("SELECT count(*) FROM character_emotion_appraisal_history WHERE source_id='resolution'").fetchone()[0] == 2


@pytest.mark.parametrize("same_timestamp", [False, True])
def test_one_model_batch_can_open_then_resolve_in_durable_source_order(stores, same_timestamp):
    _, emotion = stores
    emotion.receive("z-opening", "练习时间还没有定。", occurred_at=NOW)
    later = NOW if same_timestamp else NOW + timedelta(minutes=1)
    emotion.receive("a-resolution", "时间已经确认好了。", occurred_at=later)
    basis = emotion.assessment(["a-resolution", "z-opening"], now=later)
    assert [s["source_id"] for s in basis["sources"]] == ["z-opening", "a-resolution"]
    assert basis["sources"][0]["source_order"] < basis["sources"][1]["source_order"]
    opening = appraisal("z-opening", "练习时间还没有定", "concerned", concern={
        "id": "practice", "action": "open", "summary": "在意时间未定"})
    resolution = appraisal("a-resolution", "时间已经确认好了", "relieved", concern={
        "id": "practice", "action": "resolve", "summary": "对于刚才提到的安排放心了"})
    assert emotion.commit(basis, {"appraisals": [resolution, opening]}) is True
    assert emotion.view(now=later)["concerns"] == []
    assert emotion.pending_source_ids() == []


def test_one_model_batch_can_withdraw_earlier_misunderstanding(stores):
    _, emotion = stores
    emotion.receive("first", "你必须现在答应。", occurred_at=NOW)
    emotion.receive("second", "刚才是在引用别人说的话。", occurred_at=NOW + timedelta(seconds=1))
    basis = emotion.assessment(["first", "second"])
    assert emotion.commit(basis, {"appraisals": [
        appraisal("first", "你必须现在答应", "hurt"),
        appraisal("second", "刚才是在引用别人说的话", "relieved",
                  revises={"source_id": "first", "action": "withdraw"}),
    ]}) is True
    assert source_ids(emotion.view(now=NOW)) == ["first"]
    assert source_ids(emotion.view(now=NOW + timedelta(seconds=1))) == ["second"]


def test_batch_future_reference_or_cycle_rolls_back_all_items(stores):
    _, emotion = stores
    emotion.receive("first", "刚才理解错了。", occurred_at=NOW)
    emotion.receive("second", "现在也有新的理解。", occurred_at=NOW + timedelta(seconds=1))
    basis = emotion.assessment(["first", "second"])
    with pytest.raises(ValueError, match="EMOTION_CONTEXT_STALE"):
        emotion.commit(basis, {"appraisals": [
            appraisal("first", "刚才理解错了", "relieved", revises={"source_id": "second", "action": "withdraw"}),
            appraisal("second", "现在也有新的理解", "concerned", revises={"source_id": "first", "action": "withdraw"}),
        ]})
    assert emotion.pending_source_ids() == ["first", "second"]
    assert emotion.view(now=NOW + timedelta(seconds=1))["reactions"] == []


@pytest.mark.parametrize("later_reaction", ["none", "calm"])
def test_delayed_prior_appraisal_requeues_implicit_context_and_survives_restart(stores, later_reaction):
    life, emotion = stores
    emotion.receive("delayed", "Practice is still uncertain.", occurred_at=NOW)
    later = NOW + timedelta(minutes=1)
    emotion.receive("later", "I understand.", occurred_at=later)
    item = appraisal("later", "I understand.", later_reaction)
    commit(emotion, item)
    commit(emotion, appraisal("delayed", "Practice is still uncertain.", "concerned", concern={
        "id": "practice", "action": "open", "summary": "Still awaiting practice confirmation"}))
    assert emotion.pending_source_ids() == ["later"]
    assert "later" not in source_ids(emotion.view(now=later))
    restarted = CharacterEmotionStore(life.path)
    assert restarted.pending_source_ids() == ["later"]
    assert restarted.commit(restarted.assessment(["later"], now=later), {"appraisals": [item]}) is True
    assert restarted.pending_source_ids() == []
    with life._db() as db:
        assert db.execute("SELECT count(*) FROM character_emotion_sources").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM character_emotion_appraisal_history WHERE source_id='later'").fetchone()[0] == 2


def test_concurrent_prior_appraisal_invalidates_unseen_implicit_context_before_commit(stores):
    _, emotion = stores
    emotion.receive("first", "I am upset.", occurred_at=NOW)
    emotion.receive("second", "I understand.", occurred_at=NOW + timedelta(seconds=1))
    snapshot = emotion.assessment(["second"])
    commit(emotion, appraisal("first", "", reported_affect={
        "subject": "user", "quote": "I am upset.", "affect": "frustrated"}))
    with pytest.raises(ValueError, match="EMOTION_CONTEXT_STALE"):
        emotion.commit(snapshot, {"appraisals": [appraisal("second", "")]})
    assert emotion.pending_source_ids() == ["second"]


def test_same_batch_plain_reaction_binds_earlier_provisional_context(stores):
    _, emotion = stores
    emotion.receive("z-first", "Practice is difficult.", occurred_at=NOW)
    emotion.receive("a-next", "Keep going.", occurred_at=NOW)
    commit(emotion, appraisal("z-first", "Practice is difficult.", "frustrated"),
           appraisal("a-next", "Keep going.", "calm"))
    assert emotion.pending_source_ids() == []
    assert source_ids(emotion.view(now=NOW)) == ["z-first", "a-next"]


def test_displayed_concern_contributors_are_bounded_without_losing_internal_evidence(stores):
    life, emotion = stores
    for index in range(30):
        key = f"detail-{index}"
        emotion.receive(key, "Practice is still uncertain.", occurred_at=NOW + timedelta(seconds=index))
        commit(emotion, appraisal(key, "Practice is still uncertain.", "concerned", concern={
            "id": "practice", "action": "open", "summary": "Awaiting practice confirmation"}))
    later = NOW + timedelta(minutes=1)
    concern = emotion.view(now=later)["concerns"][0]
    assert len(concern["source_ids"]) <= 24
    assert concern["source_count"] == 30
    emotion.receive("resolution", "Practice is confirmed.", occurred_at=later)
    basis = emotion.assessment(["resolution"])
    for public in (basis["concerns"][0], basis["source_contexts"]["resolution"]["concerns"][0]):
        assert len(public["source_ids"]) <= 24 and public["source_count"] == 30
    commit(emotion, appraisal("resolution", "Practice is confirmed.", "relieved", concern={
        "id": "practice", "action": "resolve", "summary": "Confirmed"}))
    assert emotion.view(now=later)["concerns"] == []
    with life._db() as db:
        assert db.execute("SELECT count(*) FROM character_emotion_appraisals").fetchone()[0] == 31


def test_pending_overflow_probe_does_not_expand_assessment_batch_limit(stores):
    _, emotion = stores
    keys = [f"receipt-{index}" for index in range(34)]
    for key in keys:
        emotion.receive(key, "Hello.", occurred_at=NOW)
    assert emotion.pending_source_ids(limit=33) == keys[:33]
    with pytest.raises(ValueError, match="EMOTION_INPUT_INVALID"):
        emotion.assessment(keys[:33])
    with pytest.raises(ValueError, match="EMOTION_INPUT_INVALID"):
        emotion.pending_source_ids(limit=34)


def test_existing_concern_continuation_cannot_bypass_late_context_change_with_own_quote(stores):
    _, emotion = stores
    for key, seconds in (("original", 0), ("late", 1), ("continuation", 2)):
        emotion.receive(key, "Practice is still uncertain.", occurred_at=NOW + timedelta(seconds=seconds))
    opening = dict(id="practice", action="open", summary="Awaiting practice confirmation")
    commit(emotion, appraisal("original", "Practice is still uncertain.", "concerned", concern=opening))
    commit(emotion, appraisal("continuation", "Practice is still uncertain.", "concerned", concern=opening))
    commit(emotion, appraisal("late", "Practice is still uncertain.", "calm"))
    assert emotion.pending_source_ids() == ["continuation"]
    assert emotion.view(now=NOW + timedelta(seconds=2))["concerns"][0]["source_ids"] == ["original"]


def test_open_concern_nothing_raised_again_leaves_the_view_but_not_the_ledger(stores):
    """挂心的事 showed a morning line for days: an open concern is shown only while recent."""
    from runtime.private_world.character_emotion import CONCERN_WINDOW
    _, emotion = stores
    emotion.receive("exam", "下周的视唱考试还没准备好。", occurred_at=NOW)
    commit(emotion, appraisal("exam", "下周的视唱考试还没准备好", "concerned", concern={
        "id": "exam", "action": "open", "summary": "视唱考试还没准备好"}))
    assert [c["id"] for c in emotion.view(now=NOW + CONCERN_WINDOW - timedelta(minutes=1))["concerns"]] == ["exam"]
    stale = NOW + CONCERN_WINDOW + timedelta(minutes=1)
    assert emotion.view(now=stale)["concerns"] == []
    # Still open in the ledger: a later source can raise it again, and it shows again.
    emotion.receive("again", "考试的事还是没底。", occurred_at=stale)
    basis = emotion.assessment(["again"])
    assert [c["id"] for c in basis["source_contexts"]["again"]["concerns"]] == ["exam"]
    emotion.commit(basis, {"appraisals": [appraisal("again", "考试的事还是没底", "concerned", concern={
        "id": "exam", "action": "open", "summary": "视唱考试还没准备好"})]})
    assert [c["id"] for c in emotion.view(now=stale)["concerns"]] == ["exam"]


def test_concern_quoting_the_users_message_says_it_is_the_users_words(stores):
    """A concern opened on a user message quotes the user's 我; bare, it read as hers."""
    life, emotion = stores
    emotion.receive("m1", "我明天要面试，好紧张。", occurred_at=NOW)
    commit(emotion, appraisal("m1", "我明天要面试，好紧张。", "concerned", concern={
        "id": "emotion:m1", "action": "open", "summary": "我明天要面试，好紧张。"}))
    note = "在家里练琴：左手两处衔接还是不稳。"
    life.publish_day("day:practice", dict(location="家里", activity="练琴", note=note), [],
                     occurred_at=NOW + timedelta(minutes=1))
    assert emotion.publish("day:practice") is True
    commit(emotion, appraisal("day:practice", note, "concerned", concern={
        "id": "emotion:day:practice", "action": "open", "summary": note}))
    view = emotion.view(now=NOW + timedelta(minutes=2))
    assert [c["summary"] for c in view["concerns"]] == ["用户说：我明天要面试，好紧张。", note]
    # Her reaction to the user's message quotes it the same way; her own life stays unlabelled.
    assert [r["quote"] for r in view["reactions"]] == ["用户说：我明天要面试，好紧张。", note]


def test_concern_written_in_her_words_is_not_labelled_as_the_users(stores):
    _, emotion = stores
    emotion.receive("m1", "约好的练习时间还没有确定。", occurred_at=NOW)
    commit(emotion, appraisal("m1", "练习时间还没有确定", "concerned", concern={
        "id": "practice-time", "action": "open", "summary": "还在意练习时间没有确定"}))
    assert [c["summary"] for c in emotion.view(now=NOW)["concerns"]] == ["还在意练习时间没有确定"]
