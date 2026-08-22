# BSide Olivia Community

这是一个面向 Windows 的社区维护版 Olivia 本地化项目，提供可审计的人格、回信、记忆、语音与媒体装配接口。

本仓库只包含项目自有源代码、公开文档、schema、合成测试数据和安装工具。它不包含原版游戏、官方媒体、模型权重、用户数据、访问凭据或第三方运行时。

## 当前能力

- 本地 HTTP 服务与回信契约；
- 可替换的 LLM、ASR、TTS、记忆和媒体适配器；
- Persona 2.0 Constitution 与 provenance 资产；
- provider 缺失时明确返回 `UNAVAILABLE` / `DEGRADED`；
- 第三方模型和原版资源通过本机路径与哈希校验引用，不进入 Git。

实验性能力及完成度见 [docs/EXPERIMENTAL_MODULES.md](docs/EXPERIMENTAL_MODULES.md)。本项目不承诺兼容官方在线服务。

## 快速开始

需要 Windows 与 Python 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python local_server.py
```

服务默认只监听 `127.0.0.1:8899`。模型、原版资源和数据目录必须由使用者在本机显式配置；不要把密钥写入源码、配置示例、命令历史或 Issue。

第三方依赖与下载入口见 [docs/THIRD_PARTY_DOWNLOADS.md](docs/THIRD_PARTY_DOWNLOADS.md)。公开与私有内容边界见 [docs/PUBLIC_REPOSITORY.md](docs/PUBLIC_REPOSITORY.md)。

## 测试

```powershell
python -m pytest -q
python baseline_hardening_scan.py --mode all
git diff --check
```

## 贡献

提交前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)、[SECURITY.md](SECURITY.md) 与 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。不要提交模型、媒体、官方资源、真实通信、用户数据、凭据、机器专属路径或运行证据。

## 许可

本仓库中的项目自有代码与文档采用 [MIT License](LICENSE)。原版游戏、官方资源、第三方模型与运行时仍受各自权利和许可证约束，不随本仓库授权。
