from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mem0_memory import Mem0Config, Mem0ConversationMemoryAdapter, Mem0AdapterError
from runtime.memory.source_retrieval import SourceRetrieval


NOW = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


class StoredMemories:
    """A scrollable local store; no embedding or model calls are available."""

    def __init__(self, count=1028):
        self.points = [
            SimpleNamespace(id=f"memory-{i}", payload={
                "user_id": "local-user", "agent_id": "linli",
                "domain": "conversation_memory", "source_id": f"reply:{i}",
                "data": "老照片里的回忆" if i == 1027 else f"用户的第 {i} 条记忆",
                "created_at": (NOW - timedelta(hours=i)).isoformat(),
                "updated_at": (NOW - timedelta(hours=i)).isoformat(),
            }) for i in range(count)
        ]
        self.scrolls = []
        self.vector_store = SimpleNamespace(
            client=self, collection_name="fixture", _create_filter=lambda value: value,
            get=lambda vector_id: next((p for p in self.points if str(p.id) == vector_id), None),
        )

    def scroll(self, *, collection_name, scroll_filter, limit, offset,
               with_payload, with_vectors):
        assert collection_name == "fixture"
        assert with_payload is True and with_vectors is False
        assert scroll_filter == {
            "user_id": "local-user", "agent_id": "linli", "domain": "conversation_memory",
        }
        self.scrolls.append(offset)
        start = offset or 0
        rows = self.points[start:start + limit]
        end = start + len(rows)
        return rows, end if end < len(self.points) else None

    def get_all(self, **kwargs):
        filters = kwargs["filters"]
        return {"results": [
            {"id": str(p.id), "memory": p.payload["data"],
             "user_id": p.payload["user_id"], "agent_id": p.payload["agent_id"],
             "created_at": p.payload["created_at"],
             "metadata": {"source_id": p.payload["source_id"], "domain": p.payload["domain"]}}
            for p in self.points if all(p.payload.get(k) == v for k, v in filters.items())
        ][:kwargs["top_k"]]}

    def add(self, messages, **kwargs):
        assert kwargs["infer"] is False
        memory_id = "manual-fixture"
        self.points.append(SimpleNamespace(id=memory_id, payload={
            **kwargs["metadata"], "user_id": kwargs["user_id"],
            "agent_id": kwargs["agent_id"], "data": messages,
            "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
        }))
        return {"results": [{"id": memory_id, "memory": messages, "event": "ADD"}]}

    def delete(self, memory_id):
        self.points = [point for point in self.points if str(point.id) != memory_id]
        return {"message": "Memory deleted successfully!"}


def adapter(tmp_path, backend=None):
    config = Mem0Config(enabled=True, data_root=tmp_path/"memory",
                        llm_base_url="http://127.0.0.1:9/v1",
                        llm_model="unused", embedding_cache=tmp_path/"models")
    return Mem0ConversationMemoryAdapter(backend or StoredMemories(), config)


def test_browser_reaches_records_beyond_the_old_thousand_record_cap(tmp_path):
    memory = adapter(tmp_path)
    result = memory.browse_memories(user_id="local-user", page=52, limit=20, now=NOW)
    assert result["total"] == result["total_count"] == 1028
    assert result["page"] == 52
    assert len(result["records"]) == 8
    assert result["records"][-1].memory_id == "memory-1027"
    assert len(memory.backend.scrolls) > 1


def test_browser_scrolls_the_real_qdrant_store_without_embedding_calls(tmp_path):
    pytest.importorskip("qdrant_client")
    import importlib.metadata
    try:
        importlib.metadata.version("mem0ai")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("optional Mem0 SDK is not installed")
    import sys
    # Existing adapter tests replace SDK modules; keep genuine imports in a
    # separate process so this check neither consumes nor pollutes those fakes.
    import subprocess
    from pathlib import Path
    check = subprocess.run([sys.executable, "-X", "utf8", "-c",
        "import sys; from pathlib import Path; sys.path.insert(0, 'tests/memory'); "
        "from test_memory_browser import _check_native_qdrant; _check_native_qdrant(Path(sys.argv[1]))", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=20)
    assert check.returncode == 0, check.stdout + check.stderr


def _check_native_qdrant(tmp_path):
    import qdrant_client as qdrant
    from mem0.vector_stores.qdrant import Qdrant
    from qdrant_client.models import PointStruct, VectorParams, Distance
    from uuid import NAMESPACE_URL, uuid5
    backend = StoredMemories()
    native = qdrant.QdrantClient(location=":memory:")
    try:
        native.create_collection("fixture", vectors_config=VectorParams(size=2, distance=Distance.COSINE))
        native.upsert("fixture", points=[PointStruct(
            id=str(uuid5(NAMESPACE_URL, str(point.id))), vector=[1.0, 0.0], payload=point.payload,
        ) for point in backend.points] + [PointStruct(
            id=str(uuid5(NAMESPACE_URL, "other-user")), vector=[1.0, 0.0],
            payload={**backend.points[0].payload, "user_id": "other-user"},
        )])
        store = Qdrant.__new__(Qdrant)
        store.client, store.collection_name = native, "fixture"
        backend.vector_store = store
        memory = adapter(tmp_path, backend)
        result = memory.browse_memories(user_id="local-user", page=52, now=NOW)
        assert result["total"] == 1028 and len(result["records"]) == 8
        assert result["records"][-1].text == "老照片里的回忆"
        def native_delete(memory_id):
            native.delete("fixture", points_selector=[memory_id])
            return {"message": "Memory deleted successfully!"}
        backend.delete = native_delete
        assert memory.delete_memory(result["records"][-1].memory_id, user_id="local-user") is True
        assert memory.browse_memories(user_id="local-user", now=NOW)["total"] == 1027
    finally:
        native.close()


def test_browser_searches_old_records_and_filters_dates_before_paging(tmp_path):
    memory = adapter(tmp_path)
    result = memory.browse_memories(user_id="local-user", query="老照片", now=NOW)
    assert result["total"] == 1
    assert result["records"][0].memory_id == "memory-1027"
    result = memory.browse_memories(user_id="local-user", days=7, now=NOW, sort="old")
    assert result["total"] == 169
    assert result["records"][0].memory_id == "memory-168"
    assert memory.browse_memories(user_id="local-user", query="没有匹配", page=50)["page"] == 1


def test_browser_rejects_an_unfiltered_or_cross_user_provider_response(tmp_path):
    backend = StoredMemories(1)
    backend.points[0].payload["user_id"] = "other-user"
    with pytest.raises(Mem0AdapterError):
        adapter(tmp_path, backend).browse_memories(user_id="local-user")


def test_old_memory_can_be_corrected_and_deleted_through_existing_admin(tmp_path):
    from conversation_memory_admin import ConversationMemoryAdminService
    memory = adapter(tmp_path)
    admin = ConversationMemoryAdminService(memory, tmp_path / "audit.sqlite3")
    result = admin.correct(memory_id="memory-1027", corrected_text="用户喜欢海边的老照片。",
                           request_id="correct-fixture", reason="用户更正")
    assert result.affected_count == 2
    assert all(record.memory_id != "memory-1027" for record in memory.all_memories(user_id="local-user"))
    assert memory.browse_memories(user_id="local-user", query="海边")["total"] == 1
    result = admin.delete(memory_id="manual-fixture", request_id="delete-fixture", reason="用户删除")
    assert result.affected_count == 1
    assert memory.browse_memories(user_id="local-user", query="海边")["total"] == 0


def test_large_clear_does_not_rescan_the_store_for_every_deletion(tmp_path):
    from conversation_memory_admin import ConversationMemoryAdminService
    memory = adapter(tmp_path)
    admin = ConversationMemoryAdminService(memory, tmp_path / "audit.sqlite3")
    result = admin.clear(request_id="clear-fixture", reason="用户清空", confirmed=True)
    assert result.affected_count == 1028 and memory.backend.points == []
    assert len(memory.backend.scrolls) < 30


def test_exact_point_delete_still_rejects_other_users_and_roles(tmp_path):
    backend = StoredMemories(1)
    memory = adapter(tmp_path, backend)
    for key, value in (("user_id", "other-user"), ("agent_id", "other-character")):
        original = backend.points[0].payload[key]
        backend.points[0].payload[key] = value
        assert memory.delete_memory("memory-0", user_id="local-user") is False
        assert len(backend.points) == 1
        backend.points[0].payload[key] = original


@pytest.mark.parametrize("kwargs", [
    {"page": 0}, {"page": True}, {"limit": 101}, {"days": 6}, {"sort": "unsafe"},
])
def test_browser_rejects_invalid_paging_options(tmp_path, kwargs):
    with pytest.raises((ValueError, Mem0AdapterError)):
        adapter(tmp_path).browse_memories(user_id="local-user", **kwargs)


def test_originals_page_full_keyword_search_source_and_user_isolation(tmp_path):
    index = SourceRetrieval(tmp_path/"originals.sqlite3")
    for i in range(1028):
        text = "更早的海边照片" if i == 1027 else f"这是一封信 {i}"
        index.put("local-user", f"reply:{i}", text, "收到你的信了。", NOW-timedelta(hours=i))
    index.put("other-user", "private", "更早的海边照片不能泄漏", "秘密", NOW)
    result = index.browse_page("local-user", page=103, limit=20)
    assert result["total"] == 2056
    assert len(result["originals"]) == 16
    result = index.browse_page("local-user", query="海边", limit=20)
    assert result["total"] == 1
    assert result["originals"][0]["source_id"] == "reply:1027"
    result = index.browse_page("local-user", source_id="reply:1027", full=True)
    assert {r["speaker"] for r in result["originals"]} == {"user", "linli"}


def test_full_original_read_keeps_long_text_and_receipt_aliases(tmp_path):
    index = SourceRetrieval(tmp_path/"originals.sqlite3")
    long = "夏天的海边" * 1300
    index.put_received("local-user", "received-user:1", long, NOW)
    index.alias_received("local-user", "reply:1", ["received-user:1"])
    preview = index.browse_page("local-user", source_id="reply:1")
    assert preview["originals"][0]["excerpt"] is True
    full = index.browse_page("local-user", source_id="reply:1", full=True)
    assert full["originals"][0]["text"] == long
    assert full["originals"][0]["excerpt"] is False


def test_qq_message_is_listed_once_beside_its_reply(tmp_path):
    index = SourceRetrieval(tmp_path/"originals.sqlite3")
    received = NOW - timedelta(minutes=2)
    index.put_received("local-user", "received-user:q1", "就是8月26号的信呀", received)
    index.put("local-user", "reply:q1:1", "就是8月26号的信呀", "我记得那天。", NOW)
    index.alias_received("local-user", "reply:q1:1", ["received-user:q1"])
    index.put("local-user", "reply:letter:1", "普通来信", "收到啦。", NOW - timedelta(hours=1))
    page = index.browse_page("local-user", limit=20)
    rows = [(r["source_id"], r["speaker"]) for r in page["originals"]]
    assert rows.count(("received-user:q1", "user")) == 1 and ("reply:q1:1", "user") not in rows
    assert ("reply:q1:1", "linli") in rows and ("reply:letter:1", "user") in rows
    assert page["total"] == 4
    family = index.browse_page("local-user", source_id="reply:q1:1", full=True)
    assert sorted((r["source_id"], r["speaker"]) for r in family["originals"]) == [
        ("received-user:q1", "user"), ("reply:q1:1", "linli")]
    assert [r["source_id"] for r in index.browse("local-user", "", 10)["originals"]].count("reply:q1:1") == 1
