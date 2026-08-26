# Contributing

感谢参与 BSide Olivia Community。贡献应保持范围清晰、可复现、可撤销，并尊重原版内容、第三方项目和用户数据的边界。

## 开始前

1. 阅读 [`README.md`](README.md)、[`docs/README.md`](docs/README.md) 和 [`docs/PUBLIC_REPOSITORY.md`](docs/PUBLIC_REPOSITORY.md)。
2. 从最新 `main` 创建独立分支和工作树。
3. 使用独立 Python 3.12 虚拟环境安装 `requirements-dev.txt`。
4. 确认模型、媒体、私人数据、`.evidence/`、凭据和机器配置没有被加入 Git。

## 变更要求

- 一个 PR 只解决一个可独立回滚的问题；代码、契约、文档和对应测试一起提交。
- 优先复用已有模块和上游项目，不为单一实现新增框架或抽象层。
- 新的公开 API、状态码或 manifest 字段必须同步 schema 和 focused 测试。
- provider 失败必须返回明确的 `UNAVAILABLE`、`DEGRADED` 或等价状态，不能用占位成功掩盖失败。
- 路径通过配置、环境变量或本地 manifest 传入，禁止提交机器绝对路径。
- fixture 必须是合成数据或已获许可的数据；真实信件、媒体、声音参考、Token 和抓包不得进入 Git。
- Issue、日志和测试失败输出不得打印请求正文、回复正文、密钥、用户数据或完整本机路径。

## 最小验证

先运行与改动直接相关的 focused 测试。公共边界或发布相关变更还应运行：

```powershell
python -m pytest -q
python baseline_hardening_scan.py --mode all
git diff --check
git status --short
```

测试记录必须写清命令、结果和边界。CI 通过不等于真实模型、GPU、原版客户端或人工视听验收通过。

## PR 说明

PR 至少说明：

- 解决的问题和明确不处理的范围；
- 变更文件与用户可见影响；
- 实际运行的测试及结果；
- 外部依赖、隐私/版权边界和未验证项；
- 失败时的回滚方式。
