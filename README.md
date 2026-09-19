# BSide Olivia Community

Olivia 社区客户端：本机信件、记忆与媒体生成，以及可选的云端 GPU 和 QQ 私聊接入。

- [客户端介绍与安装](client/README.md)
- [下载发布版本](https://github.com/Ornn8/bside-olivia-community/releases)
- [使用与开发文档](client/docs/README.md)
- [更新记录](client/CHANGELOG.md)
- [贡献指南](CONTRIBUTING.md) · [安全问题](SECURITY.md) · [行为准则](CODE_OF_CONDUCT.md)
- [许可证](LICENSE) · [第三方声明](client/THIRD_PARTY_NOTICES.md)

## 仓库结构

| 目录 | 内容 |
| --- | --- |
| `client/` | 完整 Python 客户端工程、依赖配置及启动入口 |
| `client/runtime/` | 私聊、信件、记忆、媒体等业务模块 |
| `client/installer/` | Windows 安装、更新和打包 |
| `client/tests/` | 自动化测试 |
| `client/tools/` | 开发与维护工具 |
| `client/docs/` | 用户文档、开发说明、版本记录 |
| `.github/` | CI、发布流程和协作模板 |

## 从源码开发

使用 Python 3.12，在仓库根目录执行：

```powershell
cd client
python -m pip install -e ".[dev]"
python -m pytest -q
```

下层文档中的命令默认在 `client/` 执行。源码安装、启动、卸载入口分别为
`client/INSTALL.cmd`、`client/START.cmd`、`client/UNINSTALL.cmd`。
发布安装包的使用方法与目录结构不变。
