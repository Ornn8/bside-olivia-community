# Contributing

感谢参与。这个项目仍处于公开前整理阶段，贡献应优先保持边界清晰、可复现和可撤销。

## 开始前

1. 阅读 [`README.md`](README.md) 和 [`docs/PUBLIC_REPOSITORY.md`](docs/PUBLIC_REPOSITORY.md)。
2. 使用独立虚拟环境安装 `requirements-dev.txt`。
3. 确认 Git 工作树中的私有资产、`.evidence/`、模型和用户数据没有被加入提交。

## 提交要求

- 一个变更只解决一个问题，避免把本机调试、模型下载和产品代码混在一起。
- 新的公开 API、状态码或 manifest 字段必须同步 schema、文档和测试。
- 所有 provider 失败都必须有明确的 `UNAVAILABLE`、`DEGRADED` 或等价状态；禁止用占位成功掩盖失败。
- 路径通过配置或环境变量传入，禁止提交机器绝对路径。
- 测试 fixture 必须是合成数据或已获许可的数据；真实信件、媒体、token 和抓包不得进入 Git。
- 不要在 issue、日志或测试失败输出中打印请求正文、回复正文、密钥或完整路径。

## 提交前检查

```powershell
python -m pytest -q
python baseline_hardening_scan.py --mode all
git diff --check
git status --short
```
提交说明应包含：变更范围、测试命令及结果、外部依赖、未验证边界和回滚方式。
