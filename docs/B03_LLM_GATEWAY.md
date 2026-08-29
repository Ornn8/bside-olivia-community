# B03：LLM Gateway 与回信编排

状态：本批实现了可替换的文本 provider、内部回信事件流和 B02 `/toy/letter/send` 接入。它不是完整产品、不是公开人格定稿，也没有发送真实请求或真实信件。

## 边界

- LLM 是唯一允许通过 API 调用的组件；HTTP 适配器只支持 OpenAI-compatible Chat Completions 与 Responses 形状。
- `mock` 是完全离线、确定性的文本 provider；`none` 是默认状态。未配置 API 时不会因为启动 core 失败，也不会偷偷访问默认地址。
- HTTP 表面继续使用 B02 `b02.v1` envelope、`/toy/*` 路径和真正的 `501` 未实现边界。当前 HTTP 没有宣称 SSE、WebSocket 或 Live；流式事件只在内部异步窄接口中提供。
- 默认运行使用 `linli_character/persona_release_v2.json`，并按实际回复模式装配 Persona 2.0。`linli_character/system_prompt.md` 仅在显式设置 `persona_v2_enabled=false` 时作为 legacy DRAFT 输入。
- B03 只通过 `memory_port.MemoryPort` 的可选窄端口读取安全分区；没有 memory 时继续使用普通文本 prompt。B04 的 `legacy_letters` 不作为当前对话记忆；新信件成功后只有在 opt-in profile 启用时才写入 `conversation_memory` 摘要/事实。B02 current 信件与 legacy 视图保持隔离。

## 安装、启用、停用

B03 没有模型权重安装步骤，也没有新的强制依赖；仓库已有的 Python 与 `aiohttp` 即可运行。

B04 的 SQLite memory 是独立可选 profile；默认不创建数据库，不依赖 AIRI Memory Alaya WIP、Mem0 包或云端调用。安装、导入、清空、卸载和资料边界见 [`B04_LOCAL_MEMORY.md`](B04_LOCAL_MEMORY.md)。

1. 复制 `contracts/llm_config.example.json` 为被 `.gitignore` 忽略的 `llm_config.json`，把无效示例域、模型占位符和 `requires_api_key` 替换为自己的设置。
2. 在进程环境中设置 `OLIVIA_LLM_API_KEY`，或把 `api_key_env` 改成另一个环境变量名。配置文件永远不应出现 key 值。
3. 使用 `provider=openai_compatible` 启用 API，`provider=mock` 启用离线回信，`provider=none` 停用。
4. 停用只需改 `provider=none` 或 `feature_enabled=false`；卸载不需要删除原版资产、用户数据或 `linli_character`，只删除本批新增的 Python 文件和配置登记即可。

可完全离线验证：

```text
OLIVIA_LLM_PROVIDER=mock
rtk python tools/healthcheck.py --profile core
rtk python tools/healthcheck.py --profile llm
```

也可用环境变量覆盖配置：`OLIVIA_LLM_BASE_URL`、`OLIVIA_LLM_MODEL`、`OLIVIA_LLM_API_STYLE`、`OLIVIA_LLM_TIMEOUT_SECONDS`、`OLIVIA_LLM_MAX_RETRIES`、`OLIVIA_LLM_RETRY_BACKOFF_SECONDS`、`OLIVIA_LLM_STREAM`、`OLIVIA_LLM_MAX_INPUT_CHARS`、`OLIVIA_LLM_MAX_OUTPUT_CHARS`、`OLIVIA_LLM_FALLBACK_PROVIDER`、`OLIVIA_PERSONA_FILE`。

## 配置与自定义 provider

机器可读配置在 `contracts/llm_config.schema.json`，安全模板在 `contracts/llm_config.example.json`。关键字段：

| 字段 | 作用 |
|---|---|
| `provider` | `none`、`mock` 或 `openai_compatible`；也可由代码注册自定义名称 |
| `base_url` / `model` | API 基址和模型名；模板使用 `.invalid` 示例域 |
| `api_key_env` / `requires_api_key` | 只保存环境变量名，不保存密钥 |
| `api_style` | `chat_completions` 或 `responses` |
| `timeout_seconds` / `max_retries` / `retry_backoff_seconds` | 单次请求超时、429/5xx/网络失败重试预算 |
| `stream` | 内部事件编排是否消费 provider 流；HTTP `/toy` 仍是非 SSE 响应 |
| `max_input_chars` / `max_output_chars` | 输入消息总长度和输出长度上限 |
| `fallback_provider` | 显式降级登记；默认 `none`，不会无提示生成占位回信 |
| persona_v2_file / persona_v2_enabled | 默认 Persona 2.0 release 文件与开关；关闭该开关才进入 legacy 路径 |
| persona_file / feature_enabled | 可选 legacy DRAFT 文件和总开关；默认配置不再引用该文件 |
| persona_config / persona_evidence_file | 候选结构化配置与短 provenance/evidence 索引；候选包默认独立关闭，不会替换 DRAFT |

代码可通过 `llm_gateway.register_provider("name", factory)` 注册 provider。factory 接收 `GatewayConfig`，返回实现 `Gateway.complete()` 与可选 `Gateway.stream()` 的对象；异常只能使用 `GatewayError` 的稳定 code/retryable 分类，不能把响应 body、header、key 或 prompt 放入异常文本。

### 必需 function call 契约

`GatewayToolCall` 是公开的不可变返回值，只有 `name: str` 与
`arguments: Mapping[str, Any]` 两个字段。需要结构化、非自由文本结果的
调用方使用：

```python
calls = await gateway.complete_with_tools(
    messages=messages,
    tools=[{
        "type": "function",
        "function": {
            "name": "apply_voice_performance",
            "parameters": {"type": "object"},
        },
    }],
    tool_choice="required",
    request_id="opaque-request-id",
)
```

- 该窄接口只接受 `tool_choice="required"`，并始终以
  `stream=false` 发起一次请求；它不会回退到自由文本。
- `api_style="chat_completions"` 发送 OpenAI Chat Completions 的
  `tools`/`tool_choice` 形状，并读取
  `choices[0].message.tool_calls[*].function`。唯一兼容例外是
  `openai_compatible` 的 `deepseek-v4-flash`：其默认 thinking 模式不接受
  `tool_choice`，因此该请求省略该字段，但仍只发送一次带唯一 function schema 的
  请求，并在本地严格要求恰好一个有效调用；不会回退为文本或接受任意调用。
- `api_style="responses"` 将同一输入函数 schema 转为 Responses 的
  `type/name/description/parameters` 形状，并读取
  `output[*]` 中 `type="function_call"` 的 `name` 和 `arguments`。
- 缺少调用、无效 name、非对象 arguments、不能解析的 arguments JSON，或调用数组
  中任何畸形 sibling 都是非重试的 `PROVIDER_PROTOCOL`；不支持该能力或 provider
  不可用仍为可重试的 `PROVIDER_UNAVAILABLE`。调用方不得把这些错误转换成占位成功。
- 自定义 provider 若声明支持此公开能力，必须覆写
  `Gateway.complete_with_tools()` 并返回 `Sequence[GatewayToolCall]`；若不支持，
  保持基类的 `PROVIDER_UNAVAILABLE` 失败边界。自定义实现也必须保留 request ID、
  不记录提示词/响应正文，并使用稳定的 `GatewayError` 分类。

## 回信事件与并发语义

`reply_orchestrator.py` 提供 `ReplyRequest`、`ReplyOrchestrator.start()`、`ReplyRun.events()` 和 `ReplyRun.cancel()`。每个 request 具有 request ID；给出相同 `idempotency_key` 且输入一致时复用同一个结果，输入不一致返回 `IDEMPOTENCY_CONFLICT`。

事件顺序和终态：

```text
request_accepted
       ↓
stream_delta (零个或多个，仅内部事件)
       ↓
completed | cancelled | retryable_error | terminal_error
```

事件队列有上限，生产者在队列满时等待，形成可测试的背压；消费方不应无限制地缓存正文。编排超时会取消当前 provider task 并发出 `retryable_error/LLM_TIMEOUT`；用户取消只发出 `cancelled`，不会伪造 `completed`。HTTP B02 先确认持久化的 `PENDING` 信件，再在后台等待非流式终态；派发前写入 `PROCESSING`，携带稳定请求/幂等标识，崩溃后的不确定调用不会自动重发。它不是原生 Live 或 SSE。

## B02 响应、错误和降级

- 正常回信仍为 HTTP `200`，并保留 `letter_id`、`letterId`、`status=COMPLETED` 等 B02 字段。
- 未配置 provider、网络失败、429/5xx 重试耗尽或超时：后台将信件置为 `FAILED`，detail 暴露脱敏错误码和 `retryable`，不写 `reply_text` 占位文本；默认 `LLM_UNAVAILABLE`/`LLM_TIMEOUT` 为可重试。
- provider 非重试 4xx：映射为 `LLM_PROVIDER_REJECTED`，不把上游 body 回显给客户端。
- provider JSON 损坏或空响应：映射为 `LLM_PROTOCOL_ERROR`，明确失败，不返回 `200`。
- 输入超长、空消息、非法角色（只允许 `system`、`user`、`assistant`）在 provider 调用前拒绝。
- `/toy/letter/resend`、分享、MIDI 和其它未实现能力继续返回真实 HTTP `501`；B03 不把它们改成成功。

错误日志只记录事件名、稳定错误码、状态、耗时/计数等脱敏元数据，不记录 prompt、response、body、query 值、完整 URL、Authorization 或环境变量值。

## 健康检查和证据

`/health?profile=core` 只检查进程内 core，LLM 未配置时仍可为 `HEALTHY`。`/health?profile=llm` 返回 provider 配置状态、模型是否配置、key 是否存在、重试/stream 能力、人格状态和 `network_called=false`；默认候选包关闭时人格仍为 `DRAFT`。它不探测 API，因此“已配置”不等于真实 provider 已经可达。

```text
rtk python tools/healthcheck.py --profile core
rtk python tools/healthcheck.py --profile llm
rtk python -m pytest -q tests/llm
```

本批测试只使用 synthetic 文本与本地 `aiohttp` mock server；不下载权重、不调用生产 provider、不发送真实用户数据。视觉证据：本批没有 UI 改动，标记 `N/A`；听觉证据：只生成文本，标记 `N/A`。
