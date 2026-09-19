# P03 原版主包到播放器的交叉绑定审计

## 1. 目的

现有证据已经确认：

- 原版 `feapp.dat` 主包读取 `letter_status` 与 `reply_content`；
- `feplayer.dat` 和 `webplayer.dat` 都读取 query string；
- 三个包中都没有已证实的 `reply_video_url`、`video_url` 或 `videoUrl` 契约；
- 两个播放器并非同一实现。

下一步不能直接修改播放器或添加新字段，而是先确定：

```text
原版 Collection
  -> 原版主包中的播放器启动桥
  -> query / 启动参数
  -> feplayer 或 webplayer
```

## 2. 新增只读工具

```text
tools/audit_original_player_bridge.py
```

输入是同一受支持版本的：

```text
feapp.dat
feplayer.dat
webplayer.dat
```

工具只在内存中读取 ZIP 成员，不解压到持久目录，不修改原版文件，也不访问网络。

## 3. 输出范围

报告只包含：

- 三个 archive 的 SHA-256；
- 受限 HTML/JavaScript 成员名称、大小和 SHA-256；
- `feplayer`、`webplayer` 等明确技术引用的计数；
- `window.open`、`loadURL`、`BrowserWindow`、`URLSearchParams` 等固定启动/传参标记的计数；
- 从 `URLSearchParams` alias、直接 `.searchParams` 和 query template 中提取的参数键；
- 只含 ASCII、无空格、命中 player/video/media/letter/reply/Collection allowlist 的技术字符串；
- 每个 player 引用附近窗口的 SHA-256 与固定 marker 集合；
- 主包和各播放器之间共同 query key 与共同技术字符串。

报告不会输出：

- HTML/JavaScript 源码；
- 任意字符串表；
- 原版文案；
- 完整 URL；
- 绝对路径；
- 用户数据；
- 凭据；
- context 原文。

## 4. 结果状态

工具只给出结构状态，不直接宣称运行行为：

### `literal_reference_with_query_contract_candidate`

主包存在播放器技术引用，并且与至少一个播放器共享 query key。

这只是候选契约，仍需核验引用上下文唯一性和真实点击行为。

### `literal_reference_without_query_contract`

主包存在播放器引用，但没有找到共同参数键。

可能原因包括：

- 参数名经过压缩或运行时组装；
- 使用位置参数；
- native 层补充参数；
- 当前静态规则仍未覆盖真实写法。

### `no_literal_main_bridge_reference`

主包没有出现 `feplayer` / `webplayer` 明文引用。

此时不能继续猜测前端锚点，下一步应转向受控的原版客户端运行行为或 native-process 调用审计。

## 5. 运行方式

拉取最新 `main` 后，在本机执行：

```powershell
python tools/audit_original_player_bridge.py `
  "<Steam安装目录>\0.0.9.615\resources" `
  --output original-player-bridge-audit.json
```

只上传生成的 `original-player-bridge-audit.json`。不要上传三个 `.dat`、解包目录或源码片段。

## 6. 后续决策

只有报告证明一个明确候选后，才进入 CLIENT-UI-02：

1. 核验主包引用是否唯一；
2. 核验它是否位于 Collection 当前信件流程；
3. 确认播放器类型和参数键；
4. 定义最小、可回滚、版本哈希绑定的补丁；
5. 先接通原版视频播放，再讨论设置和管理入口。

在此之前继续冻结：

- standalone Control Center 发布入口；
- 新视频页面；
- 新 `LetterDetail` 路由；
- 对 `reply_video_url` 等字段的未经证实接线；
- 对播放器 archive 的直接修改。
