# 20251221 超声—漏磁同步数据回放程序

这是一个 Windows 原生桌面回放程序，按照项目根目录的《数据同步回放方案》实现。主程序使用 PyQt5 直接读取本地数据，不经过浏览器、HTTP 或 JSON：

- 超声位于上层；
- 漏磁位于下层；
- 漏磁默认显示 Left/Right 共16行，每行叠加 X/Z；
- 横轴是以接头为零点的相对距离，单位为米；
- 超声使用 ID 作为横向位置，虚拟 Mileage 不参与统一横轴；
- 四段漏磁和七段超声通过清单与窗口查询作为一个整体回放；
- 原始 DAT、CSV 和超声二进制分片保持不变。

## 目录

~~~text
数据同步回放程序/
├─ server.py
├─ config.json
├─ README.md
├─ 启动回放.bat
├─ desktop_app.py
├─ requirements-desktop.txt
├─ build_desktop.bat
├─ 启动桌面回放.bat
├─ model_result_store.py
└─ web/
   ├─ index.html
   ├─ styles.css
   └─ app.js
~~~

## 启动

也可以直接双击目录中的 `启动回放.bat`。

### 原生桌面版

推荐直接双击 `启动桌面回放.bat`。如果尚未生成 exe，该脚本会用当前 Python 环境直接启动 `desktop_app.py`。

首次运行源码版需要 Python、NumPy 和 PyQt5。桌面窗口默认尺寸约为 1280×820，可自行调整上下图区域。

启动后可在左侧“回放”页的“当前窗口信息”中点击“选择数据并加载…”。选择完整的
“分发软件版本”目录，或直接选择其中的 `data\\数据集名` 文件夹，程序会在当前窗口切换
漏磁、超声、模型结果和人工标注。每个数据集会在自己的目录保存 `config.json`
（例如 `data\20251221\config.json`）。
软件首次加载该数据时会从公共配置初始化；之后超声接头 ID、同步偏移等配置按数据集分别保留。
公共 `config.json` 不会被改写。同步偏移可在软件中调整并保存；如需修改 `joint.us_id` 等接头参数，
可直接编辑对应日期目录中的 `config.json`。移动或备份数据时请一并保留该配置文件。

漏磁 DAT 采用内存映射打开，不会在启动时整体读入内存。启动校验仍会检查文件完整性和各通道记录数，
通道索引对齐改为抽查每个文件首部、中部和尾部的短连续区间，避免在大文件上反复进行大量随机磁盘读取。

生成 Windows 软件：

~~~powershell
python -m pip install -r requirements-desktop.txt
build_desktop.bat
~~~

生成后的程序位于 `dist\UltrasoundMFLReplay\UltrasoundMFLReplay.exe`。打包时不复制原始 DAT、CSV 或超声二进制数据，软件仍按 config.json 读取外部数据目录。

### 旧 Web 版

旧 Web 版仍保留用于回退，启动方式如下：

在 PowerShell 中执行：

~~~powershell
cd E:\trae\ultrasoundMFLReply_app
python server.py --open
~~~

不自动打开浏览器时：

~~~powershell
python server.py
~~~

然后访问：

~~~text
http://127.0.0.1:18765/
~~~

桌面版和旧 Web 版默认读取：

~~~text
漏磁：
E:\trae\ultrasoundMFLReply_app\分发软件版本\data\20251221\漏磁

超声：
E:\trae\ultrasoundMFLReply_app\分发软件版本\data\20251221\超声\data
~~~

如果超声输出目录不同，可以修改 config.json 中的 ultrasound_data_dir。路径可以是绝对路径，也可以是相对于本程序目录的路径。

桌面版也支持将 `ultrasound_data_dir` 指向包含 `*_S0.csv`、`*_S1.csv` 等主文件的目录；程序首次启动时会自动生成 `chunk_*.bin` 和 `manifest.json` 缓存，后续直接读取缓存。也可以在 config.json 中设置 `ultrasound_csv_dir` 指定 CSV 目录，并用 ultrasound_data_dir 指定缓存目录。

## 当前工作锚点

config.json 中暂时使用已有接头验证脚本对应的工作锚点：

~~~text
漏磁段：EX01
漏磁 record_pos：584064
该 record_pos 对应的原始 DAT index：230378
超声 ID：2794006
统一尺度：0.57462 mm/单位
~~~

程序实际使用漏磁原始 DAT index 进行空间换算。record_pos 只用于定位工作接头位置，避免把记录序号误当成物理位置。

如果后续确认了更准确的接头中心，只需修改 config.json 中的 joint 配置，不需要改动原始数据。

## 操作

- 播放/暂停：顶部播放按钮或空格键；
- 前后移动：左右方向键；
- 小步移动：Shift 加左右方向键；
- 首页/末尾：Home、End；
- 一键定位接头：点击“一键定位接头”按钮。程序在后台扫描全部漏磁分段和通道，直接定位 `abs(DAT.value)` 的全局最大值，并将窗口居中到该位置。超声数据只作为当前数据包的参考，不参与本次定位；
- 检测超声接头：点击“检测超声接头”按钮。程序扫描超声回波，在约 1 米窗口中比较预设接头 Gate 覆盖、双 Track 和回波数量，给出候选 ID 与置信度。确认后会写入当前数据配置的 `joint.us_id` 并重载坐标；这是候选检测，仍建议结合超声图像人工确认；
- 双击任意图区域：将该位置居中；
- 鼠标移动：上下图同步显示竖直位置线；
- 窗口宽度：支持5、8、10、20、50、100米；
- 超声 Track0/Track1 可分别开关；
- GateNo 可通过顶部按钮筛选；
- 漏磁始终保持左右16行，可通过横向窗口查看不同位置。

## 人工标注

桌面版支持在漏磁面板上直接框选异常区间。操作流程如下：

1. 点击“开始框选漏磁区间”。
2. 在下方漏磁图上按住鼠标左键拖过异常波形。
3. 选择“焊缝”“斜裂纹”或“振动干扰”，点击“保存异常标注”。
4. 看完一个窗口后点击“当前窗口已检查”，登记人工检查覆盖范围。

修改已有标注：

1. 在下方漏磁图中点击已有的彩色标注区域，或点击侧栏的已有标注列表项。
2. 软件会加载该标注的范围、异常类型、方向和左右侧范围；修改类型后直接点击“更新异常标注”。
3. 如果需要修改范围，点击“重新框选范围”，在漏磁图上重新拖选，再点击“更新异常标注”。
4. 点击“取消编辑”后重新拖选，保存时会新建一条标注；删除旧标注需要先在列表中选中后点击“删除选中标注”。

更新会保留原来的 annotation_id，不会因为修改而产生重复事件。

标注文件写入软件基准目录的 `分发软件版本\\annotations\\20251221\\`：

- `event_labels.csv`：只记录异常事件；每行保存 `EX 段`、`start_record_pos`、`end_record_pos`、DAT index 和统一坐标。跨段事件会使用同一个 `annotation_id` 的多行保存。
- `reviewed_ranges.csv`：记录哪些原始区间已经人工检查。只有“已检查区间 - 异常标注 - 排除区间”的补集才可以作为正常样本，未检查区间不能直接当作正常。
- `excluded_ranges.csv`：记录接头、采集故障等不参与判定的区间，避免把它们混入正常样本。
- `manifest.json`：记录原始数据路径、32 通道顺序、DAT 记录格式和坐标配置，便于后续读取复现。

标注不复制原始波形。后续程序可按 `mfl_segment`、`start_record_pos`、`end_record_pos` 打开对应 DAT，从 `leftxchannel1` 读取 DAT index，并按 `manifest.json` 中的 `channel_order` 取出原始 32 通道值。

项目内已提供无界面读取类，训练或统计脚本可以直接使用：

~~~python
from src.data.annotation_dataset import MFLAnnotationDataset

labels = MFLAnnotationDataset("分发软件版本/annotations/20251221")
for event in labels.iter_events():
    values = event["spans"][0]["values"]  # (32, N), 原始 DAT.value，int16
    dat_index = event["spans"][0]["dat_index"]
~~~

## 模型结果叠加查看

桌面版启动后会按 config.json 的 `model_result_path` 自动读取模型结果 CSV，并将事件区间映射到与回放相同的“接头相对距离”横轴，同时叠加到超声和漏磁面板。默认配置读取项目根目录的：

~~~text
outputs/anomaly_scoring/v1/candidate_events.csv
~~~

左侧“模型异常候选 / 频率提示”中可以：

- 开关模型结果图层；
- 按“全部异常候选”“左侧候选”或“右侧候选”筛选；
- 对含 `frequency_hint` 字段的结果，按“频率提示：振动干扰/焊缝”筛选；
- 使用“上一处/下一处”跳转并居中查看；
- 直接点击图上的模型结果框查看事件编号、异常区间、DAT record 和异常分数；
- 通过“选择结果文件…”临时加载另一份 CSV，或用“重新加载”刷新当前文件。

当前阶段仍不把模型结果写入 `annotations`，也不把频率提示当作最终真值。由
`src.analysis.detect_frequency_events` 生成的 `candidate_events.csv` 会额外包含
`frequency_hint`、`frequency_peak_cpr`、`frequency_confidence` 等字段；软件会把它
们显示为“频率提示”，供人工复核后再用人工标注功能保存正式标签。该算法的频率
中心应随着人工标注增加而重新校准，当前只有少量样本，不能替代人工确认。当前
结果主要覆盖 EX01，若模型输出来自其他漏磁段，需要在 CSV 中提供 `mfl_segment`
字段，或修改 config.json 的 `model_result_segment`。历史
`candidate_events_full_v2.csv` 中的规则分类字段暂不采用。

## 数据读取策略

漏磁使用 NumPy 内存映射，按窗口读取 EX01～EX04 的32个通道，不会把约43.8 GB原始数据全部加载到内存。

超声读取现有的12字节二进制分片，并按 ID 范围查询。当前二进制文件虽然按虚拟 Mileage 分片，但 Mileage 只用于存储分片，页面横轴仍然使用接头相对 ID 坐标。

漏磁曲线按每个分片保留首尾点和 X/Z 极值，使用 min/max 包络降采样，以保护窄峰和强响应。超声保持散点语义，不连接成连续波形。

## 注意事项

- mfl_segment_gaps_m 默认全部为0，表示暂按 EX01→EX04 顺序连续组织；如确认段间存在物理空档，应在对应 EX 段配置 gap_before_m。
- EX01尾部已知可能存在连续零值或采集终止现象，程序会在元数据中标记，不自动当作缺陷。
- 超声 Track0/Track1 与漏磁 Left/Right 的业务对应关系仍应由专业人员确认；程序不隐式硬编码这种对应。
- 当前漏磁模型数据集和异常事件主要覆盖 EX01，回放原始四段不等于已有模型结果覆盖四段。
- 关闭服务使用 PowerShell 中的 Ctrl+C。

## 2026-09-08 桌面版更新

- 异常类型支持焊缝、斜裂纹、振动干扰、疑似斜裂纹、未知异常；新增类型编码分别为 `suspected_diagonal_crack` 和 `unknown_anomaly`。沿用原有 CSV 字段、原始记录索引以及标注编辑/跳转方式。
- 左侧开启“测量两点距离”后，在超声或漏磁中依次点击两个信号，显示共享标定横轴上的沿线路距离。再次点击开始新测量，清除按钮清空结果；测量不会写入标注文件。
- 超声上股 Track 1 在上，下股 Track 0 在下。
- 漏磁选区及已有标注框下方显示尺寸箭头和宽度；窗口外的完整区间标为“全宽”。小于 1 m 显示毫米，其他显示米。
- 测距精度依赖里程比例、接头对齐和人工选点；不使用超声原始里程，不测量深度差。
