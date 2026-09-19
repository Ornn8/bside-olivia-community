# P03-03A Companion Control Center 本地管理界面

## 1. 决策

陪伴类产品的 PrivateWorld、长期记忆、音乐校准、数据管理和故障诊断，不以 CLI 作为正式用户入口。

P03 新增一个仅在本机运行的 **Companion Control Center**：

- Windows 安装后可从开始菜单和安装目录直接打开；
- 使用浏览器承载本地界面，但不连接任何外部网站；
- 与原版客户端兼容 API 使用独立端口和管理会话；
- 用户不需要输入命令、编辑 JSON 或配置环境变量；
- CLI 只保留给自动化测试和维护者，不作为产品完成条件。

## 2. 产品定位

Control Center 不是新的聊天客户端，也不是林离人格的一部分。

它承担系统层操作：

```text
配置与健康状态
PrivateWorld 候选确认和世界状态管理
长期记忆查看、纠正、删除和导出
MiniMax 音乐校准与盲听评分
数据备份、迁移和删除
本地故障诊断
```

原版客户端继续承担沉浸式书信体验。Control Center 只在用户主动打开时出现，不在对话中插入系统面板或技术字段。

## 3. 技术方案

### 3.1 本地 Web UI

第一版使用：

- aiohttp 独立 loopback 管理站点；
- 仓库内静态 HTML、CSS 和 ES Modules；
- 不使用外部 CDN、字体、分析脚本或图片；
- 不要求 Node.js 常驻服务；
- 构建产物随 Python wheel / 安装包分发；
- 中文为默认界面语言。

允许开发阶段使用小型构建工具，但最终运行时只需要静态资源，不依赖 npm 服务。

### 3.2 独立权限边界

建议端口：

```text
原版客户端兼容 API：127.0.0.1:8899
Control Center：       127.0.0.1:8900 或动态可用端口
```

两者不共享 mutation 权限。

原版客户端可以读取书信和媒体状态，但不能访问：

- PrivateWorld 控制视图；
- 记忆管理；
- 管理候选；
- 数据导出和删除；
- 音乐校准；
- 配置中的秘密和路径。

## 4. 会话与安全

### 4.1 启动流程

```text
用户点击“Olivia Control Center”
  -> launcher 确认本地后端运行
  -> 生成一次性 bootstrap token
  -> token 只放在 URL fragment
  -> 浏览器加载无敏感数据的本地 shell
  -> JavaScript 将 fragment token POST 到 session endpoint
  -> 后端验证并立即作废 token
  -> 设置 HttpOnly + SameSite=Strict session cookie
  -> 前端清除 fragment
```

URL fragment 不会随普通 HTTP 请求发送给服务器，降低 token 进入访问日志或浏览历史同步的风险。

### 4.2 Mutation 安全

- 所有 mutation 需要有效管理 session；
- 所有 mutation 需要 CSRF token；
- session 有空闲超时；
- 退出页面可主动注销；
- bootstrap token 只能使用一次；
- 后端只绑定 loopback；
- 拒绝 Host header 和 Origin 异常；
- 不向原版前端 origin 开放 CORS；
- Content-Security-Policy 默认 `default-src 'self'`；
- 禁止外部脚本、字体、图像和网络请求；
- 日志不记录管理 token、正文、记忆文本、PrivateWorld statement 或完整请求体。

### 4.3 高风险确认

下列操作需要二次确认：

- 关系阶段变更；
- 住所权限提高；
- control-only 事实改为 character-known；
- 清空全部长期记忆；
- 重置或删除 PrivateWorld；
- 删除 Archive；
- 删除全部本地数据。

删除全部数据要求用户输入界面显示的短确认词，不使用固定公开口令。

## 5. 信息架构

### 5.1 首页

展示卡片：

- 核心服务；
- 主模型；
- Persona；
- 长期记忆；
- PrivateWorld；
- 说话视频；
- 音乐视频；
- 最近一次错误；
- 待确认建议数量。

状态只使用：

```text
可用
降级
不可用
未配置
正在处理
```

不把“文件存在”显示为“功能可用”。

### 5.2 待确认建议

统一展示：

- PrivateWorld conflict / repair / boundary candidate；
- 未来允许的记忆纠正建议；
- 过期候选。

每项包含：

- 简短说明；
- 来源时间；
- 关联 letter/reply 引用；
- 预计影响；
- 批准、拒绝、稍后处理。

候选不得直接执行。

### 5.3 关系与边界

包括：

- 当前关系阶段；
- 定性 familiarity / trust / comfort / closeness / tension；
- 最近事件时间线；
- 称呼授权和撤销；
- 住所权限；
- 冲突与修复历史。

默认不显示原始 0–100 分数，不提供任意数值编辑。

### 5.4 私人世界线

包括：

- Local Continuation 列表；
- 新增、编辑、删除；
- `control_only / pending / character_known`；
- 清楚区分“系统知道”和“林离知道”；
- 变更记录和来源。

### 5.5 长期记忆

包括：

- 搜索；
- 按时间和相关度排序；
- 查看来源、时间和 memory id；
- 删除单条错误记忆；
- 纠正记忆；
- 手工添加明确事实；
- 清空新对话记忆；
- 导出；
- 暂停 Mem0 写入；
- 查看 Archive 与 Mem0 分域状态。

“纠正”流程：

```text
选择错误记忆
  -> 展示原内容和来源
  -> 用户填写正确事实
  -> 删除旧记忆
  -> 以 local_user_correction 来源写入新事实
  -> 记录审计映射
```

不能直接修改 Qdrant 文件。

### 5.6 音乐校准

提供 P03-01C 本机实验入口：

- 选择固定合成样本集；
- 选择 Caption 版本、CFG 和 seed 组；
- 排队生成；
- 显示进度和稳定错误码；
- 播放音频；
- 盲听评分；
- 隐藏当前 seed 和参数直到评分提交；
- 查看汇总；
- 选择生产 profile；
- 导出脱敏评分摘要。

不显示或上传真实私人来信。

### 5.7 数据与隐私

按域操作：

```text
当前信件
Archive
长期记忆
PrivateWorld
媒体
配置
日志
全部本地数据
```

支持：

- 查看占用空间；
- 导出；
- 创建备份；
- 恢复测试副本；
- 删除；
- 显示默认卸载会保留哪些数据。

### 5.8 诊断

展示脱敏信息：

- 版本和 commit；
- health profiles；
- provider 名称；
- 模型配置是否完整；
- 媒体依赖缺失项；
- 最近稳定错误码；
- 队列状态；
- 数据库 schema 版本；
- 本地修复建议。

不显示：

- API key；
- 完整绝对路径；
- 私人正文；
- 模型原始响应；
- PrivateWorld 原始 payload。

## 6. 管理 API

建议路径：

```text
GET  /control/api/status
POST /control/api/session/bootstrap
POST /control/api/session/logout

GET  /control/api/private-world/snapshot
GET  /control/api/private-world/events
GET  /control/api/private-world/candidates
POST /control/api/private-world/candidates/{id}/approve
POST /control/api/private-world/candidates/{id}/reject
POST /control/api/private-world/relationship-stage
POST /control/api/private-world/nicknames
POST /control/api/private-world/home-access
POST /control/api/private-world/continuations

GET    /control/api/memory
POST   /control/api/memory/manual
POST   /control/api/memory/correct
DELETE /control/api/memory/{id}
POST   /control/api/memory/export
POST   /control/api/memory/clear

GET  /control/api/music-calibration/runs
POST /control/api/music-calibration/runs
POST /control/api/music-calibration/{run}/{sample}/score
POST /control/api/music-calibration/{run}/select-profile

POST /control/api/data/export
POST /control/api/data/backup
POST /control/api/data/delete
```

API 返回稳定 schema 和错误码。UI 不解析 Python 异常文本。

## 7. 后端结构

```text
control_center/
  ├─ app.py
  ├─ auth.py
  ├─ routes_status.py
  ├─ routes_private_world.py
  ├─ routes_memory.py
  ├─ routes_music.py
  ├─ routes_data.py
  ├─ schemas.py
  └─ static/
     ├─ index.html
     ├─ app.js
     ├─ api.js
     ├─ components/
     └─ app.css
```

业务规则仍位于：

- PrivateWorld Command Service；
- ConversationMemoryPort / Mem0 Adapter；
- MiniMax calibration service；
- installer/config service。

Control Center 只是 UI 和 Adapter，不复制 reducer、记忆或媒体逻辑。

## 8. Windows 集成

安装后创建：

- `Start Olivia`；
- `Olivia Control Center`；
- `Olivia Diagnostics`；
- `Uninstall Olivia`。

快捷方式应使用无控制台窗口启动方式。用户不需要打开 PowerShell、CMD 或 Python。

第一次安装完成后自动打开 Setup Wizard；日常启动只打开原版客户端，不自动弹出 Control Center。

## 9. 可访问性与交互

- 所有主要操作可用键盘完成；
- 使用语义 HTML；
- 焦点状态清楚；
- 不只依赖颜色表达状态；
- 确认对话框说明具体影响；
- 错误信息提供可执行下一步；
- 长列表分页或虚拟化；
- 不用动画模拟“亲密度上涨”；
- 不使用诱导、施压或游戏化设计。

## 10. PR 拆分与顺序

### CONTROL-01：管理站点 shell 与安全会话

- 独立 loopback listener；
- bootstrap token；
- session、CSRF、CSP；
- 静态 shell；
- 未认证边界测试。

### CONTROL-02：首页与健康状态

- 状态卡；
- 稳定错误码；
- 无秘密日志。

### CONTROL-03：PrivateWorld 页面

依赖 PW-01 至 PW-04。

### CONTROL-04：长期记忆页面

依赖 MEM-01 至 MEM-05。

### CONTROL-05：音乐校准页面

依赖 MUSIC-01 至 MUSIC-04。

### CONTROL-06：数据、备份和诊断

接入按域导出、删除和健康检查。

### CONTROL-07：Windows 快捷方式与 Setup Wizard 接线

与 P03-05 安装器共同完成。

## 11. 测试

### 自动化

- auth/session/CSRF；
- Host/Origin 检查；
- 未授权访问；
- API schema；
- UI 静态资源不引用外部域；
- 无秘密和私人正文进入日志；
- mutation 幂等；
- destructive 二次确认；
- 键盘导航基础测试；
- Windows 路径和快捷方式。

### 本机验收

- 用户从开始菜单打开；
- 不出现终端窗口；
- 可以批准候选；
- 可以管理称呼和世界线；
- 可以搜索和删除记忆；
- 可以完成 MiniMax 盲听评分；
- 可以导出和删除指定数据域；
- 原版客户端无法调用管理 mutation；
- 后端重启后 Control Center 能重新建立安全会话。

## 12. 回滚

- Control Center 不可用时，书信正文和原版客户端继续运行；
- 管理站点可只读降级；
- 回滚不删除 PrivateWorld、Mem0 或媒体数据；
- 管理 API 版本化；
- 内部测试命令只用于恢复，不作为用户替代方案。

## 13. 完成条件

- 用户可以不接触终端完成所有管理和校准任务；
- Control Center 只在 loopback 提供服务；
- 原版客户端没有管理权限；
- PrivateWorld、Memory、音乐校准和数据管理均有可用页面；
- 所有 mutation 经过现有 Service 层；
- 高风险操作有二次确认；
- UI 不泄露秘密、私人正文和隐藏状态；
- Windows 开始菜单快捷方式可用；
- 自动化与本机验收通过。