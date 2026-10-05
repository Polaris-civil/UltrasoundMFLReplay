# 超声—漏磁同步回放

## 使用与拷贝

直接双击 `分发软件版本/软件/UltrasoundMFLReplay.exe`。
换电脑时拷走整个 `分发软件版本` 文件夹，包括软件、data、annotations、model_results 和各自的配置。目标电脑不需要安装 Python。不要只拷 EXE；`_internal` 是运行所需依赖。

## 项目目录

- `分发软件版本/`：唯一正式便携分发目录，包含软件、数据及标注。
- 根目录 Python 文件、`assets/`、`web/`：程序源码及资源。
- `tests/`：仍用于回归验证的测试。
- `docs/`：详细使用说明、发布说明和历史迁移资料。
- `archive/`：历史资料，保留备份，不用于日常启动。
- `build/`：可重新生成的构建缓存。
- `release-assets/`：线上发布的 ZIP；不需要随便携目录拷走。

## 构建与发布

在项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_desktop.ps1
powershell -ExecutionPolicy Bypass -File .\package_update_release.ps1
```

构建直接更新 `分发软件版本/软件` 的 EXE 和 `_internal`，保留已有配置、数据及标注。临时打包目录在成功后移除，不再保留第二份 dist 程序。
版本号在 `version_update.py` 的 `APP_VERSION`；发布 ZIP 固定为 `release-assets/UltrasoundMFLReplay-windows-x64.zip`。

源码运行：`python desktop_app.py --config config.json`。
回归验证：`python tests/test_version_update.py`、`python tests/test_responsive.py`、`python tests/test_portable_no_data.py`。
