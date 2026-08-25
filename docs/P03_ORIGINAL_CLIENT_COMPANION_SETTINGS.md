# P03 原版客户端内陪伴设置入口

## 1. 决策

原版 Olivia 客户端继续作为唯一用户产品外壳。

陪伴系统的长期记忆和 PrivateWorld 管理入口放入原版 `/settings` 页面，不创建：

- 独立浏览器 Control Center；
- 第二套桌面客户端；
- 新的前端路由；
- iframe；
- 要求普通用户执行的 CLI。

## 2. 原版依据

受支持客户端 `0.0.9.615` 已确认存在：

```text
/settings  -> SettingsView
Collection -> 原版信箱
webplayer  -> 原版视频播放链
```

`SettingsView` 已有 `tp-settings-item` 分区，以及 `button`、`link`、`switch`、`select` 等现有交互形式。因此本地陪伴入口只在该页面追加一个同结构分区。

## 3. 本 PR 的范围

新增：

```text
patch_companion_settings.py
```

它在隔离安装副本的 `feapp.dat` 中：

1. 核验唯一的原版主模块标签；
2. 保持 `assets/main-917d29fc.js` 及其他既有资源逐字节不变；
3. 只修改 `index.html`；
4. 新增一个仓库自有的本地 bootstrap；
5. 在 `/settings` 中追加“本地陪伴”分区；
6. 在原版窗口内打开“长期记忆 / 私人世界”弹层外壳；
7. 只允许连接显式的 `127.0.0.1` 或 `localhost` HTTP 端口；
8. 失败时恢复修改前的归档。

## 4. 用户界面边界

入口沿用原版设置页已有样式类：

```text
tp-settings-item
text-text-body
text-title-m
text-label-l
rounded-full
border-grey-5
```

弹层：

- 留在原版窗口内；
- 可通过关闭按钮、背景点击或 Escape 退出；
- 使用语义化 dialog / tab / tabpanel；
- 不加载外部脚本、字体或页面；
- 不在此 PR 中复制 Memory 或 PrivateWorld 业务逻辑。

## 5. 后端契约

本地陪伴状态和视频回信开关共用同一 loopback 服务：

```text
GET /toy/companion/status
POST /toy/companion/settings/video-reply
```

状态响应使用 `p03.original-companion-read.v1`，其中 `capabilities.video_reply` 为 `{enabled, default_enabled}`。开关 mutation 使用 `p03.original-companion-mutation.v1`，请求为 `{enabled, request_id, reason}`，成功返回 `APPLIED`、`NOOP` 或 `DUPLICATE`。

同一 `request_id` 与相同 payload 重放原始结果；不同 payload 返回 `409 VIDEO_REPLY_REQUEST_CONFLICT` 且不改变状态。稳定错误包括 `VIDEO_REPLY_ENABLED_INVALID`、`VIDEO_REPLY_REQUEST_CONFLICT`、`VIDEO_REPLY_REPLAY_INVALID`、`VIDEO_REPLY_SETTINGS_INVALID`、`VIDEO_REPLY_SETTINGS_UNAVAILABLE` 和 `COMPANION_REQUEST_ID_INVALID`。

如果状态或 mutation 尚未接线，界面显示“本机陪伴服务暂不可用”，不会伪造成功。开关在信件接收边界冻结资格：关闭后的新信只能文字、不排队媒体；已接收信不受之后切换影响。

现有已接线能力包括：

```text
CLIENT-SETTINGS-02  状态与只读数据 API
CLIENT-SETTINGS-03  长期记忆查看、纠正和删除
CLIENT-SETTINGS-04  PrivateWorld 快照、候选和受控修改
CLIENT-SETTINGS-05  安装器接线与原版客户端真实验收
```

所有写操作必须继续经过现有 Memory Service、PrivateWorld Command Service、Reducer 和 Ledger，前端不能直接写 SQLite 或 Qdrant。

## 6. 安全与回滚

- 只补丁隔离安装副本；
- 独立保留 `feapp.dat.companion.orig`；
- ZIP 成员路径必须安全；
- 原版主模块缺失或锚点不唯一时停止；
- 重复运行保持幂等；
- API 地址变化时停止，不静默改写既有安装；
- 原有成员除 `index.html` 外必须保持相同哈希；
- 新 bootstrap 不包含 iframe、`window.open` 或外部 URL；
- 任一验证失败时恢复开始前的归档。

## 7. 完成条件

- 真实 `0.0.9.615` 原版归档可通过补丁验证；
- 原版主模块 SHA-256 保持不变；
- 设置入口只在 `/settings` 出现；
- 不创建新路由或独立页面；
- 同一配置重复执行不重复注入；
- 非 loopback API 地址全部拒绝；
- 备份、回滚和归档安全测试通过；
- `public-smoke` 与 hardening scan 通过。
