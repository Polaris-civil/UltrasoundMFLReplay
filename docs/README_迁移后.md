# 标注软件独立项目

双击根目录 `启动桌面回放.bat`，启动现有正式分发程序。源码启动：

```powershell
cd E:\trae\ultrasoundMFLReply_app
python -m pip install -r requirements-desktop.txt
python desktop_app.py --config config.json
```

旧 Web 版：`python server.py --config config.json`，访问 http://127.0.0.1:18765/。

软件源码、web、assets、配置及构建脚本均在本目录根部。portable-release包含正式 EXE、原始数据、人工标注与模型候选。archive/original-project 保存旧构建、发布暂存与历史分发产物；outputs 保存软件测试备份。

训练代码继续位于 ../magnetic_flux_leakage_analysis/src，软件不导入或依赖训练代码。训练分析读取这里的原始数据及标注；训练生成的新模型候选可通过软件“选择结果文件”加载。打包运行 build_desktop.bat；更新发布脚本默认读取根部 dist。

原先的便携包测试引用不存在的 0917软件，现已改为建立独立临时便携副本。所有正式标注 CSV 与标定配置保持原有内容。
