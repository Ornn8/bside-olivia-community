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

本 PR 只预留只读状态路径：

```text
GET /toy/companion/status
```

如果该路径尚未接线，界面显示“本机陪伴服务暂不可用”，不会伪造成功。

后续 PR 依次接入：

```text
CLIENT-SETTINGS-02  状态与只读数据 API
CLIENT-SETTINGS-03  长期记忆查看、纠正和删除
CLIENT-SETTINGS-04  PrivateWorld 快照、候选和受控修改
CLIENT-SETTINGS-05  安装器接线与原版客户端真实验收
```

视频回信设置补充契约：

- `GET/POST /toy/settings/video-reply` 读写原版 Settings 中的“视频回信”开关；
- POST 必须携带布尔 `enabled` 与独立命名空间的 `request_id`；同 ID 同 payload 重放原结果，不同 payload 返回冲突；
- GET 使用闭合的 `available {state, enabled, effective_enabled, ready, dependencies}` / `unavailable {state, reason_code}` variant；`enabled` 始终表示用户持久偏好，`effective_enabled` 表示依赖门禁后的实际状态，不能因依赖缺失覆写用户偏好；mutation result 只允许 `APPLIED/NOOP/DUPLICATE`，不可用、依赖缺失或冲突不得伪造成功；
- 初始设置与后续 Settings 共用同一能力面板；已有校验清单的能力可在客户端安装，其中 BGE 自动模式优先使用 ModelScope 固定提交并以 Hugging Face 固定提交回退；尚未冻结完整 Windows BOM 的视频依赖只向 bootstrap 返回来源 ID/标签，由本机后端白名单解析后在系统浏览器打开国内/官方来源页，并提供“重新检测”，不得伪装成自动安装成功；
- 新信在服务端接收边界冻结开关快照；后续恢复、重试和媒体处理只读该信件快照，历史媒体不受设置变化影响。

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
