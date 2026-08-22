# Repository layout

这份说明帮助维护者判断哪些目录属于公开基线，哪些目录仍是本机实验代码。

## 公开基线

| 路径 | 责任 | 公开条件 |
| --- | --- | --- |
| `http_contract.py`, `local_server.py` | 本地 HTTP 契约与服务 | 不依赖官方安装、模型或真实凭据 |
| `media_state/` | 媒体状态与资源引用边界 | 只使用逻辑 ID、manifest 和合成 fixture |
| `contracts/` | JSON schema 与接口样例 | 不包含真实请求、签名或用户数据 |
| `tests/http/`, `tests/media/` | 公开基线回归 | 可在干净 Windows + Python 3.12 环境运行 |
| `docs/`, `README.md` | 维护与发布说明 | 不记录机器绝对路径、密钥或抓包内容 |

## 实验模块

以下路径当前仍在研发或组合验证阶段。它们可以保留在工作树中供本地开发，但在通过独立的干净克隆门禁前，不应被当作默认安装目标：

- `asr/`、`tests/asr/`、`tools/asr_manage.py`：ASR provider 与 native/fallback 路径；
- `runtime/packaging/`、`tests/packaging/`、`tools/verify_*.py`：模块安装、scope 和跨 tranche 组合验证；
- `tests/live/`、`tests/live_driver/`、`tools/livetalking_*.py`：Live 编排与视觉运行时；
- `docs/B05_STREAMING_ASR.md`、`docs/B11_VISUAL_RUNTIME.md`：实验模块的设计和证据边界。

这些模块的共同要求是：无硬编码本机路径、无凭据、provider 缺失时 fail-closed，并能在无官方资源的环境中完成 fixture 测试。当前 packaging/scope 组合仍有历史基线与 child contract 的失败项，因此不放入 `public-smoke` 默认门禁。

## 永不进入公开提交

`.gitignore` 中的官方安装目录、`CosyVoice/`、`LiveTalking/`、模型/缓存/媒体、`.evidence/`、`PROTOCOL.md`、`llm_config.json`、真实 letter 与用户数据库，均属于本机或私有发布边界。若某项功能需要这些内容，公开代码只能提供配置接口、校验器和脱敏 fixture。

## 维护入口

新贡献先读 [`CONTRIBUTING.md`](../CONTRIBUTING.md)、[`SECURITY.md`](../SECURITY.md) 和 [`PUBLIC_REPOSITORY.md`](PUBLIC_REPOSITORY.md)。发布前按 [`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md) 复核依赖、许可证、路径和测试边界。
