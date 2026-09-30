# UltrasoundMFLReplay

Windows 桌面软件，用于同步浏览和回放漏磁与超声检测数据。程序只处理用户本地选择的数据文件；仓库和更新包不包含检测数据、标注或模型结果。

## 功能

- 同步显示漏磁通道与超声记录，支持播放、定位和窗口浏览。
- 对大文件采用按需读取，并对屏幕显示数据降采样。
- 支持本地标注、模型结果叠加及数据集配置。
- 可在软件设置中检查 GitHub Release 更新；更新时保留本机配置和数据。

## 运行源码

需要 Windows、Python 3.9，以及 `requirements-desktop.txt` 中的依赖。

```powershell
python -m pip install -r requirements-desktop.txt
python desktop_app.py
```

启动后在软件中选择包含漏磁数据的目录；如有超声、标注或模型结果，可按软件提示选择或配置。检测数据需要由使用者自行准备，仓库不提供这些文件。

## 构建 Windows 便携版

```powershell
build_desktop.bat
```

构建结果位于 `dist/UltrasoundMFLReplay/`。打包过程不复制原始检测数据。

## 发布更新

发布步骤和资产格式见 [UPDATE_RELEASE.md](UPDATE_RELEASE.md)。更新资产使用 `UltrasoundMFLReplay-windows-x64.zip`，压缩包只包含程序 EXE 和 `_internal/` 依赖目录；用户配置和检测数据保留在本机。
