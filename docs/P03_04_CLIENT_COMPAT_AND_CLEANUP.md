# P03-04 原版客户端兼容、验收资产迁移与旧 PR 清理

## 1. 目标

在不修改原版前端业务逻辑的前提下，让本地后端返回原版客户端能正确理解的终态；同时迁移仍有价值的 Persona 验收资产，关闭已经落后或重复的旧 PR。

## 2. 当前问题

### 2.1 状态类型不兼容

本地后端内部使用：

```text
PENDING
COMPLETED
FAILED
CANCELED
```

原版客户端部分接口按数值枚举判断：

```text
1 = pending
4 = replied
5 = failed
```

如果公开列表和详情直接返回内部字符串，前端可能一直显示“回信中”，即使正文已经生成。

### 2.2 内外模式需要同时保留

内部模式已经细分：

```text
text_letter
spoken_video
musical_video
```

原版客户端只认识：

```text
text
video
```

公开接口必须同时做到：

- 保持原版 wire value；
- 提供调试和本地管理使用的 `reply_mode_exact`；
- 不让未发布的延迟文字信提前暴露正文或模式。

### 2.3 旧 PR 已漂移

- #18：功能仍需要，但分支远落后当前 `main`；
- #25：增加另一套 Reviewer/Rewriter，与当前运行实现重复；
- #26：包含有价值的 Persona 来源覆盖和陪伴验收资产，但基线陈旧。

## 3. 公开状态映射

新增单一纯函数：

```python
def public_letter_status(letter: Mapping[str, object], *, published: bool) -> int:
    ...
```

映射：

| 内部状态 | 已到公开时间 | 公开状态 |
| --- | --- | --- |
| `PENDING` | 任意 | `1` |
| `COMPLETED`，正文未到 `reply_not_before` | 否 | `1` |
| `COMPLETED`，正文已公开 | 是 | `4` |
| `FAILED` | 任意 | `5` |
| `CANCELED` | 任意 | `5` |
| 未知值 | 任意 | `5`，并记录安全错误码 |

内部 store、ReplyPipeline 和错误处理继续使用字符串，不把兼容枚举反向写回状态机。

## 4. 接口范围

必须统一修改：

- `/toy/letter/list`；
- `/toy/letter/detail`；
- `/toy/letter/unread_count` 相关统计逻辑；
- `/toy/letter/send` 返回；
- idempotency 重放结果；
- 重启后载入旧 state 的兼容迁移。

列表和详情应保持：

```json
{
  "letter_status": 4,
  "reply_type": 1,
  "reply_mode": "video",
  "reply_mode_exact": "spoken_video",
  "media_status": "PENDING|PROCESSING|COMPLETED|UNAVAILABLE"
}
```

`letter_status=4` 只表示正文终态可用，不代表视频已经完成。视频状态由 `media_status` 单独表达。

## 5. 延迟发布语义

文字信延迟期间：

- 内部正文可以已经完成并持久化；
- 公开 `letter_status=1`；
- `reply_type=0`；
- `reply_text` 和 `reply_content` 为空；
- `reply_mode=text`；
- `reply_mode_exact=text_letter`；
- 到达 `reply_not_before` 后再返回 `4` 和正文。

视频回复当前不使用文字信延迟，正文完成后可立即显示 `4`，媒体继续异步生成。

## 6. 错误与重试

公开失败统一：

```text
letter_status = 5
reply_type = 0
```

同时保留稳定错误码：

- `LLM_TIMEOUT`；
- `LLM_UNAVAILABLE`；
- `LLM_PROVIDER_REJECTED`；
- `LLM_PROTOCOL_ERROR`；
- `REPLY_QUALITY_BLOCKED`。

媒体失败不能把已完成正文的 `letter_status` 改成 `5`。媒体错误只进入：

```text
media_status = UNAVAILABLE
media_error_code = ...
```

## 7. 旧状态文件迁移

载入 `state.json` 时兼容：

- 旧字符串 `video` -> `musical_video`，因为旧路径全部走音乐视频；
- `text` -> `text_letter`；
- 已完成但 `media_status=PROCESSING` -> `QUEUED`；
- 数值状态只在读取时转回内部枚举，不继续存储数值；
- 未知值标记为失败，不删除正文和原始记录。

迁移后使用原子写回，不覆盖损坏文件；解析失败时保留原文件并启动空 store，同时报告明确错误。

## 8. Persona 验收资产迁移

从旧 #26 迁移并适配：

```text
docs/P02_PERSONA_SOURCE_COVERAGE.md
linli_character/persona_source_coverage_v2.json
tools/persona_companion_acceptance.py
tests/persona/test_companion_acceptance.py
```

适配要求：

- 来源 Markdown 的已登记 SHA 与当前仓库一致；
- 第 00–24 节都有明确运行时落点；
- 三种当前模式和 `future_im` 风格均可验证；
- 当前三模式路由字段被识别；
- 不输出 Prompt、私人称呼、地址和 Local Continuation 实例；
- 不调用外部模型；
- 工具可在安装后的仓库外环境运行。

## 9. Reviewer 重复实现清理

对 #25：

1. 对比 `reply_runtime_models.py` 与当前 `reply_model_quality.py`；
2. 迁移当前缺失的测试：
   - payload 不含数据库、路径、密钥和 hidden score；
   - Reviewer JSON 非法时诚实降级；
   - Rewriter 最多调用一次；
   - 使用同一个精确 mode；
3. 不迁移重复运行时；
4. 关闭 #25，注明主干现有实现已覆盖。

## 10. PR 拆分与顺序

### COMPAT-01：公开数值状态映射

修改：

- `local_server.py`；
- `tests/http/test_contract.py`；
- `tests/http/test_reply_delay_and_media.py`。

不处理旧 PR 和 Persona 资产。

### COMPAT-02：状态文件迁移与重启回归

只处理持久化兼容和损坏文件边界。

### COMPAT-03：迁移 Persona 来源覆盖和验收工具

从最新 `main` 重新提交四个文件，更新到当前路由和 Persona。

### COMPAT-04：吸收 #25 缺失测试

只迁移有价值测试，不新增第二套实现。

### COMPAT-05：关闭旧 PR 和更新 Tracker

在前四个 PR 合并后：

- 关闭 #18，引用 COMPAT-01/02；
- 关闭 #25，引用当前 Reviewer 和 COMPAT-04；
- 关闭 #26，引用 COMPAT-03；
- 更新 Issue #27 的验收清单。

## 11. 测试矩阵

- PENDING 列表和详情；
- COMPLETED 延迟前与延迟后；
- FAILED 和 CANCELED；
- text、spoken video、musical video；
- 正文完成但媒体 pending/failed；
- idempotency 重放；
- 重启载入旧模式值；
- 重启恢复媒体任务；
- 未知状态；
- 原版客户端请求字段；
- Persona acceptance 工具安装后运行；
- 旧 PR 资产不造成重复模块。

## 12. 回滚

- 状态映射是公开 Adapter，可单独 revert；
- 内部字符串状态不变，因此回滚不需要数据库迁移；
- 原始 state 迁移前保留备份；
- Persona 验收资产只读，不影响运行时；
- 关闭旧 PR 前必须确认替代 PR 已合并。

## 13. 完成条件

- 原版客户端能显示等待、完成和失败；
- 延迟文字信不会提前公开；
- 视频媒体失败不影响完成正文；
- 内部三模式和公开 text/video 均正确；
- 旧 state 可恢复；
- Persona 来源覆盖和验收工具进入最新主干；
- #18、#25、#26 均有明确替代并关闭；
- 不存在两套 Reviewer/Rewriter 生产实现；
- `public-smoke` 与专项测试通过。
