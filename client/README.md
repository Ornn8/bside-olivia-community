# Olivia 客户端工程

[项目介绍、功能与安装说明](../README.md) · [开发文档](docs/README.md) · [仓库结构](docs/REPOSITORY_LAYOUT.md)

本目录是 Python 工程根目录。使用 Python 3.12，在本目录执行：

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python baseline_hardening_scan.py --mode all
```

安装、启动与卸载入口分别为 `INSTALL.cmd`、`START.cmd`、`UNINSTALL.cmd`。
安装前请阅读仓库首页的资源与环境要求。
