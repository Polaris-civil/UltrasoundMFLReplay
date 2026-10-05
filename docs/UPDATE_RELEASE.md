# GitHub 自动更新发布说明

软件更新从公开 GitHub 仓库的最新正式 Release 检查版本。默认仓库是 `Polaris-civil/UltrasoundMFLReplay`；Windows 便携版也可在左侧“设置 → 软件更新”中改成其他 `owner/repository` 并保存。首次发布带更新功能的版本仍需手动安装一次；之后可在软件内下载并安装新版。

## 发布一个版本

1. 在 `version_update.py` 中把 `APP_VERSION` 改为新版本号，例如 `2026.10.01`。
2. 在 `数据同步回放程序` 目录运行 `build_desktop.ps1`。
3. 运行 `package_update_release.ps1`。默认从 `??????/??` 打包，并在 `release-assets/` 生成 `UltrasoundMFLReplay-windows-x64.zip`。
4. 在 GitHub 创建公开 Release，标签使用 `v` 加版本号，例如 `v2026.10.01`。
5. 上传同名 ZIP 作为 Release 资产。ZIP 根目录必须直接包含 `UltrasoundMFLReplay.exe` 和 `_internal/`。

GitHub Release 页面中的版本标签与程序里的 `APP_VERSION` 应一致。更新器会比较版本号中的数字，因此使用 `vYYYY.MM.DD` 或 `v主版本.次版本.修订版本` 格式；正式 Release 才会被检查。

## 更新包内容

更新包只放程序文件：

```text
UltrasoundMFLReplay.exe
_internal/
```

不要将 `config.json`、`update_settings.json`、`data/`、`annotations/`、模型结果、整个“portable-release”目录放进更新 ZIP。更新器会在程序退出后替换 EXE 和 `_internal`，保留用户本机的配置与数据；下载的资产会校验 GitHub 提供的 SHA-256（Release API 提供时），并检查 ZIP 路径和目录结构。

更新器设置保存在 EXE 旁边的 `update_settings.json`。安装目录应允许当前用户写入；不要将便携版放在需要管理员权限的 `Program Files` 下。
