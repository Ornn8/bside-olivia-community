# P03 原版设置页陪伴只读界面

## 1. 目标

在原版 Olivia `/settings` 的“本地陪伴”弹窗中显示已经接入同一进程的真实数据：

- 长期记忆状态、列表与搜索；
- PrivateWorld 定性关系状态；
- 已授权称呼与住所权限；
- Local Continuation 及其 awareness；
- 待确认的关系建议。

用户不打开外部浏览器，不使用独立 Control Center，也不运行 CLI。

## 2. 接口

界面调用四个 GET 读取接口，并对用户明确切换的视频回信偏好发出一个 POST：

```text
/toy/companion/status
/toy/companion/memory
/toy/companion/private-world
/toy/companion/private-world/candidates
POST /toy/companion/settings/video-reply
```

视频开关请求体为 `enabled`、`request_id`、`reason`，带确认 header；同一 request ID 重放原始结果（包括 `DUPLICATE`），不同 payload 显示稳定冲突错误。UI 只把可用的 `capabilities.video_reply.enabled` 作为当前状态；不可用状态显示失败并保持原值，默认/旧配置缺失时显示开启。

切换只影响之后进入服务端的新信。接收边界记录的资格不随之后切换改变；关闭资格的信件纯文字且不显示媒体等待，开启资格的信件继续原有视频流程。

## 3. 长期记忆

长期记忆面板显示：

- 能力是否可用；
- 当前记录数量；
- 最多 50 条记忆正文；
- 本机创建时间；
- 最多 500 字符的搜索词。

界面不显示 memory ID、source ID、user scope、向量、完整 metadata、provider 配置或系统提示词。

所有记忆正文使用 `textContent` 写入 DOM。记忆内容不能生成 HTML、脚本或原版组件。

## 4. PrivateWorld

PrivateWorld 面板只显示：

- 关系阶段；
- 熟悉、信任、自在、亲近、紧张的低／中／高；
- 已授权私人称呼；
- 住所权限；
- Local Continuation 正文与 awareness；
- 待确认关系建议的类型、摘要和时间。

不显示：

- 隐藏的 0–100 数值；
- 候选置信度；
- 来源信件与回复版本；
- command、decision 或审计 ID；
- 数据库路径和内部错误。

关系建议在本 PR 中只能查看，不能批准或拒绝。

## 5. 失败与降级

读取能力分别降级，视频开关写入失败也不影响 Collection、文字回信或媒体播放：

- Memory 不可用时，只显示长期记忆暂不可用；
- PrivateWorld 不可用时，不影响 Memory；
- 候选不可用时，不影响 PrivateWorld 摘要；
- 整体状态接口不可用时，两个面板都显示暂不可用；

## 6. 客户端补丁升级

原版主 JavaScript 继续保持字节不变。补丁只管理：

```text
index.html 中唯一的本地脚本标签
assets/olivia-companion-settings.js
```

已经存在旧版 repository-owned bootstrap 时：

1. 验证脚本路径、标记和 API base；
2. 只替换本项目拥有的 bootstrap 与版本属性；
3. 保留原版主包和其他资源；
4. 失败时恢复修改前归档；
5. API base 不一致时拒绝更新。

## 7. 安全边界

- API base 必须是带端口的 loopback HTTP；
- 无 iframe、外部窗口、外部脚本、字体或分析服务；
- 无 `innerHTML`、`document.write`、`eval` 或动态 Function；
- 所有数据以文本节点显示；
- 请求 `no-store`，不带浏览器凭据；
- 搜索和列表数量有固定上限；
- Node.js 语法检查在可用环境中执行。

## 8. 后续

```text
CLIENT-SETTINGS-06  记忆纠正/删除与候选批准/拒绝
CLIENT-INSTALL-01   安装器自动应用原版设置补丁
CLIENT-CONTRACT-02  Collection 数据格式生产接线
```
