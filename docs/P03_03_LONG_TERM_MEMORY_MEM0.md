# P03-03 Mem0 OSS 长期记忆适配

## 1. 决策

新对话长期记忆采用 `mem0ai/mem0` 的开源 Python Library，通过本仓库的 Adapter 接入；不复制 Mem0 的事实提取、记忆编排、检索融合或时间处理算法。

选择依据：

- 项目成熟、持续活跃，当前约 6.3 万 GitHub stars；
- Apache-2.0；
- Python 原生接入；
- 支持自定义 OpenAI-compatible LLM；
- 支持 Hugging Face 本地 embedding；
- 支持本地 Qdrant path，无需强制云服务；
- 支持 add、search、CRUD 和 metadata filters；
- 可嵌入现有运行时，而不要求替换整个 Agent harness。

未选择：

- Letta：当前路线是完整 stateful agent runtime，会与现有 Persona、ReplyPipeline、PrivateWorld 和媒体编排重叠；
- Zep：当前仓库更偏 Graphiti、示例与平台集成，不如 Mem0 适合作为本地 Python 应用中的窄记忆 Adapter。

该选择仍通过 MEM-01 spike 验证。若当前锁定版本无法满足本地、删除、过滤或中文检索要求，则停止集成并回到选型门，而不是在本仓库补写 Mem0 缺失算法。

## 2. 目标

完成后，新对话会形成受控的长期记忆：

```text
用户来信 + canonical reply
        ↓
结构化 exchange
        ↓
Mem0 事实提取与存储
        ↓
下一封来信前检索
        ↓
受限 memory references
        ↓
PersonaAssembly
```

同时保持三个数据域完全分离：

| 数据域 | 所有者 | 内容 |
| --- | --- | --- |
| Archive | 现有只读 SQLite | 原始旧信、原回复和来源信息 |
| Long-term Memory | Mem0 OSS | 新对话中提取的用户事实、偏好、经历和近期主题 |
| PrivateWorld | 独立 ledger | 关系数值、阶段、称呼、住所权限和 Local Continuation |

## 3. 非目标

- 不把原始旧信批量灌入 Mem0；
- 不把 Persona、system prompt 或 evidence 当用户记忆；
- 不把 PrivateWorld hidden score、control-only fact 或管理命令写入 Mem0；
- 不在本仓库实现新的事实提取、冲突合并、时间推理或向量数据库；
- 不要求 Mem0 Cloud；
- 不因记忆失败阻断回信；
- 不用记忆摘要覆盖原始档案。

## 4. 当前实现问题

现有 `MemoryPort.remember_conversation()` 接收人为拼接的 summary 和 facts，运行时会写入：

```text
User sent a new letter: ...
Assistant completed a reply: ...
```

这会在 Mem0 之前先做一次简化和改写，使成熟记忆框架无法根据原始 user/assistant 交换提取事实、主体和时间。

现有 SQLite conversation memory 可以保留为降级和迁移源，但不再继续扩展其自研编排逻辑。

## 5. Port 调整

不直接破坏旧接口。新增窄协议：

```python
class ConversationMemoryPort(Protocol):
    enabled: bool

    def search_context(
        self,
        query: str,
        *,
        user_id: str,
        limit: int,
    ) -> tuple[MemoryRecord, ...]: ...

    def remember_exchange(
        self,
        *,
        user_message: str,
        assistant_message: str,
        occurred_at: datetime,
        source_id: str,
        user_id: str,
    ) -> MemoryWriteResult: ...

    def list_memories(self, *, user_id: str) -> tuple[MemoryRecord, ...]: ...

    def delete_memory(self, memory_id: str, *, user_id: str) -> bool: ...

    def clear_user(self, *, user_id: str) -> int: ...

    def export_user(self, *, user_id: str) -> dict[str, object]: ...
```

现有 `MemoryPort` 逐步拆为：

```text
ArchivePort
ConversationMemoryPort
PersonaEvidencePort
```

兼容期使用 `CompositeMemoryPort` 适配现有调用者，避免一个大 PR 同时修改所有模块。

## 6. Mem0 Adapter

新增 `mem0_memory.py`：

```text
Mem0ConversationMemoryAdapter
UnavailableMem0MemoryPort
NullConversationMemoryPort
```

### 6.1 `remember_exchange`

传给 Mem0：

```python
messages = [
    {"role": "user", "content": user_message},
    {"role": "assistant", "content": assistant_message},
]

memory.add(
    messages,
    user_id=user_id,
    agent_id="linli",
    metadata={
        "source_id": source_id,
        "occurred_at": occurred_at.isoformat(),
        "domain": "conversation_memory",
        "canonical": True,
    },
)
```

要求：

- 只在 canonical reply 完成后调用；
- 同一 `source_id` 幂等；
- user 和 assistant 角色不反转；
- 不传 system prompt、Reviewer payload 或 PrivateWorld；
- 写入在工作线程执行；
- 超时或异常只记录隐私安全状态。

### 6.2 `search_context`

```python
memory.search(
    query=query,
    filters={
        "AND": [
            {"user_id": user_id},
            {"agent_id": "linli"},
            {"domain": "conversation_memory"},
        ]
    },
    top_k=12,
)
```

Adapter 转为现有 `MemoryRecord`，只向 Prompt Builder 暴露：

- memory id；
- 事实文本；
- occurred_at / created_at；
- score；
- source_id；
- domain。

Prompt 最终只选少量高相关记录，并严格限制字符预算。

### 6.3 管理操作

用户必须可以：

- 查看记忆；
- 删除单条记忆；
- 清空新对话记忆；
- 导出新对话记忆；
- 停用 Mem0 但保留数据；
- 完整卸载并选择是否删除数据。

删除和清空通过 Mem0 正式 API，不直接操作 Qdrant 内部文件。

## 7. 本地 provider 配置

### 7.1 LLM

Mem0 提取使用独立配置，但默认复用当前 OpenAI-compatible endpoint 和 API key：

```text
OLIVIA_MEMORY_LLM_PROVIDER=openai
OLIVIA_MEMORY_LLM_BASE_URL=<same or override>
OLIVIA_MEMORY_LLM_MODEL=<same or cheaper model>
OLIVIA_MEMORY_LLM_API_KEY_ENV=<env name>
OLIVIA_MEMORY_LLM_TIMEOUT_SECONDS=20
```

参数：

- temperature：0.1；
- 低并发；
- 无自动无限重试；
- 失败不阻断回复；
- 不在日志中输出 request/response body。

模型调用费用不是约束，但记忆写入不得延迟 canonical text 的公开。

### 7.2 Embedding

第一版候选：

```text
provider: huggingface
model: BAAI/bge-small-zh-v1.5
embedding_dims: 512
model_kwargs.device: cpu
```

理由：

- 中文为主要通信语言；
- 本地可运行；
- 体积和 CPU 成本适合 Windows 单机；
- 不与 MiniMax、TTS 和视频链争用 GPU。

可选高质量档：

```text
BAAI/bge-m3
```

但只有专项检索评测证明收益足以覆盖下载、内存和启动成本时才成为默认。

Embedding 模型必须固定 revision 或哈希，并由安装器显式下载；服务启动不得悄悄联网下载。

### 7.3 Vector Store

使用 Qdrant local path：

```text
<OLIVIA_LOCAL_DATA_ROOT>/memory/mem0/qdrant
```

配置：

```json
{
  "provider": "qdrant",
  "config": {
    "path": "...",
    "collection_name": "olivia_conversation_memory_v1",
    "embedding_model_dims": 512,
    "on_disk": true
  }
}
```

无需 Docker 或独立 Qdrant 服务。历史数据库和 Mem0 其他状态放在同一 `memory/mem0/` 根下，但 Archive SQLite 保持独立目录。

## 8. 配置文件

新增本地配置示例：

```json
{
  "enabled": true,
  "provider": "mem0",
  "user_id": "local-user",
  "context_max_chars": 2400,
  "write_timeout_seconds": 30,
  "search_timeout_seconds": 8,
  "llm": {
    "provider": "openai",
    "model": "configured-memory-model"
  },
  "embedder": {
    "provider": "huggingface",
    "model": "BAAI/bge-small-zh-v1.5",
    "device": "cpu",
    "embedding_dims": 512
  },
  "vector_store": {
    "provider": "qdrant",
    "collection_name": "olivia_conversation_memory_v1",
    "on_disk": true
  }
}
```

真实路径和凭据不提交到仓库。

## 9. 数据进入规则

允许进入 Mem0：

- 用户明确陈述的个人事实；
- 稳定偏好；
- 用户经历和计划；
- 双方当前交流中已明确确认的非控制事实；
- assistant 已完成的行动或承诺，前提是 canonical reply 确实包含。

不得进入 Mem0：

- system prompt；
- Persona declarations；
- Reviewer 结果；
- 质量分数；
- API key、路径和内部错误；
- PrivateWorld 数值与权限；
- control-only continuation；
- 未发布候选回复；
- 原始旧信全库；
- 用户仅作为假设、小说、测试样本或引用文本提到的内容。

Mem0 仍可能提取错误事实，因此必须提供用户可见管理和删除，而不能把框架输出当作绝对真相。

## 10. Archive 与检索编排

Prompt Builder 同时查询：

```text
Conversation Memory（Mem0）
Archive（只读旧信）
```

但输出分区明确：

```xml
<memory_context>
  <current_memories>...</current_memories>
  <archive_references>...</archive_references>
</memory_context>
```

规则：

- Archive 原文优先于 Mem0 摘要；
- Mem0 记忆与 Archive 冲突时，不自动断言任何一方为真；
- Prompt 中保留 source_id；
- 记忆缺失不等于事件没发生；
- 无证据时禁止使用“上次”“你以前总是”等补史表达。

## 11. 迁移现有 SQLite conversation memory

不自动迁移。

提供显式工具：

```text
python -m memory_admin migrate-sqlite-to-mem0 --dry-run
python -m memory_admin migrate-sqlite-to-mem0 --yes
```

迁移流程：

1. 导出现有 conversation memory；
2. 标记来源为 `legacy-local-conversation-memory`；
3. 逐条或按原 exchange 关系重放；
4. 保存迁移映射和失败记录；
5. 不删除旧数据库；
6. 用户确认后才允许归档旧 conversation table。

旧信 Archive 永不通过此工具迁移。

## 12. PR 拆分与顺序

### MEM-01：Mem0 OSS 可行性 Spike

独立实验分支，验证：

- 锁定版本可安装到 Python 3.12；
- OpenAI-compatible LLM 可用；
- Hugging Face embedding 可用；
- Qdrant local path 持久化；
- 中文 add/search；
- delete/clear/export；
- 重启后恢复；
- 无 provider 时明确失败。

只提交测试和结论，不接生产链。

### MEM-02：拆分 ConversationMemoryPort

新增窄协议和兼容 `CompositeMemoryPort`，不引入 Mem0 依赖。

### MEM-03：Mem0 Adapter

新增 `mem0_memory.py`、配置加载、状态、CRUD 和单元测试。

Mem0 依赖放入可选 extra：

```text
pip install .[memory-mem0]
```

核心安装无 Mem0 时仍可启动。

### MEM-04：检索接入 PersonaAssembly

修改 `MemoryPromptBuilder`，将 Mem0 和记忆 Archive 分区、排序和限长。

### MEM-05：canonical exchange 写入

在正文保存后异步调用 `remember_exchange`。证明：

- Reviewer 前候选不写入；
- 重写前文本不写入；
- 相同 revision 不重复；
- 写入失败不影响正文和媒体。

### MEM-06：管理 CLI 与迁移工具

增加 list/delete/clear/export/migrate/status。

### MEM-07：默认启用决策

在真实回归集验收后，将 Mem0 从可选 provider 改为默认新对话记忆层。未达到门槛时保持 opt-in，不伪造完成。

## 13. 评测

使用完全合成的多轮书信集，至少覆盖：

- 姓名、工作地点和稳定偏好；
- 临时计划与已完成事件；
- 同一事实的更新；
- 相互矛盾的陈述；
- 假设、玩笑和引用文本；
- 用户纠正错误记忆；
- 中英文混合；
- 与 Archive 原文冲突；
- 不应记忆的 PrivateWorld 和系统内容。

指标：

| 指标 | 目标 |
| --- | --- |
| 相关事实召回率 | >= 0.85 |
| 无关记忆进入 Prompt 的比例 | <= 0.15 |
| 明显错误事实写入率 | <= 0.05 |
| PrivateWorld/control 泄漏 | 0 |
| 重启后记忆恢复率 | 1.00 |
| 单条删除成功率 | 1.00 |
| 记忆 provider 失败时正文成功率 | 1.00 |

这些指标只针对固定测试集；不得把 Mem0 Cloud 的公开 benchmark 当作本地配置的实测成绩。

## 14. 隐私与日志

- 默认完全本地 Qdrant；
- 只有配置的 LLM endpoint 接收当前 exchange；
- embedding 默认本地；
- 日志不包含消息正文、记忆正文、向量、API key 或本地路径；
- health 只返回 provider、状态、计数和错误码；
- 导出文件由用户显式指定；
- 删除操作需要确认；
- 卸载默认保留数据，只有明确选项才删除。

## 15. 回滚

- `OLIVIA_MEMORY_PROVIDER=sqlite|none` 可关闭 Mem0；
- 回滚代码不删除 Qdrant path；
- Archive 始终可独立工作；
- Mem0 失败时回到无长期记忆或旧 SQLite 降级；
- 不允许在回滚中把 Mem0 内容反向覆盖 Archive。

## 16. 完成条件

- Mem0 OSS 版本已锁定并通过 spike；
- 本仓库没有复制 Mem0 内部算法；
- 新对话通过结构化 exchange 写入；
- 中文检索、重启、删除、导出和清空可用；
- Archive、Mem0、PrivateWorld 三域隔离；
- PrivateWorld 和系统信息泄漏为零；
- provider 失败不影响 canonical text；
- 用户可管理错误记忆；
- 固定回归集达到门槛；
- `public-smoke`、可选依赖安装和专项测试通过。

## 17. 上游参考

- `mem0ai/mem0`：Apache-2.0；
- Mem0 OSS Python Library；
- Mem0 自定义 LLM、Hugging Face embedder 和 Qdrant local path 配置；
- Mem0 官方文档明确区分 Library、Self-hosted Server 和 Cloud，本项目只使用 Library。
