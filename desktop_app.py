from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from bisect import bisect_left, bisect_right
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Optional

import numpy as np

from PyQt5.QtCore import (
    QSize,
    QMutex,
    QPoint,
    QPointF,
    QThread,
    Qt,
    QTimer,
    QWaitCondition,
    QRectF,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QIcon,
    QKeySequence,
    QPixmap,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QProgressBar,
    QSizePolicy,
    QShortcut,
    QSpinBox,
    QSlider,
    QStackedWidget,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from server import DataLoadCancelled, ReplayService
from annotation_store import (
    ANNOTATION_LABELS,
    AnnotationStore,
)
from model_result_store import ModelResultStore
from version_update import (
    APP_VERSION,
    DEFAULT_GITHUB_REPOSITORY,
    ReleaseCheckWorker,
    ReleaseDownloadWorker,
    UpdateError,
    start_windows_update_installer,
    validate_repository,
    validate_update_archive,
)


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = APP_DIR / "config.json"
if getattr(sys, "frozen", False):
    executable_dir = Path(sys.executable).resolve().parent
    source_app_dir = executable_dir.parent.parent
    # A copied distribution must always use the config beside the EXE. Keep
    # the source-tree fallback only for older development bundles that do not
    # carry an external config file next to the executable.
    if (executable_dir / "config.json").exists():
        APP_DIR = executable_dir
    elif (source_app_dir / "config.json").exists():
        APP_DIR = source_app_dir
    else:
        APP_DIR = executable_dir
    DEFAULT_CONFIG = APP_DIR / "config.json"


def _read_json_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_config(path: Path, value: dict[str, Any]) -> None:
    """Atomically write a UTF-8 JSON config beside its eventual destination."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _merge_config_dicts(
    defaults: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(defaults)
    for key, value in overrides.items():
        default_value = merged.get(key)
        if isinstance(default_value, dict) and isinstance(value, dict):
            merged[key] = _merge_config_dicts(default_value, value)
        else:
            merged[key] = value
    return merged


def _resolve_config_path(value: Any, base: Path) -> Optional[Path]:
    if not value:
        return None
    path = Path(str(value)).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _portable_config_path(path: Path, base: Path) -> str:
    try:
        return Path(os.path.relpath(str(path.resolve()), start=str(base.resolve()))).as_posix()
    except ValueError:
        return str(path.resolve())


def _contains_mfl_data(path: Path) -> bool:
    """Return whether a directory looks like an MFL segment directory."""
    if not path.is_dir():
        return False
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    if any(item.is_file() and item.suffix.lower() == ".dat" for item in children):
        return True
    for child in children:
        if not child.is_dir():
            continue
        try:
            if any(
                item.is_file() and item.suffix.lower() == ".dat"
                for item in child.iterdir()
            ):
                return True
        except OSError:
            continue
    return False


def _find_mfl_data_path(path: Path) -> Optional[Path]:
    """Find MFL data directly in ``path`` or in one child folder.

    A date package may temporarily contain only the MFL child while its
    matching ultrasound package is still being prepared.  The old detection
    then fell back to the default dataset instead of honoring the selection.
    """
    if _contains_mfl_data(path):
        return path.resolve()
    if not path.is_dir():
        return None
    try:
        children = [item for item in path.iterdir() if item.is_dir()]
    except OSError:
        return None
    for child in children:
        if _contains_mfl_data(child):
            return child.resolve()
    return None


def _contains_ultrasound_data(path: Path) -> bool:
    """Return whether a directory looks like an ultrasound data folder."""
    if not path.is_dir():
        return False
    return any(
        candidate.exists()
        for candidate in (
            path / "manifest.json",
            path / "data" / "manifest.json",
            path / "csv",
            path / "data",
        )
    )


def _find_nested_dataset_layout(
    path: Path,
) -> Optional[tuple[Path, Path]]:
    """Find ``<date>/<mfl>`` and ``<date>/<ultrasound>`` children."""
    if not path.is_dir():
        return None
    try:
        children = [item for item in path.iterdir() if item.is_dir()]
    except OSError:
        return None
    mfl_path: Optional[Path] = None
    ultrasound_path: Optional[Path] = None
    for child in children:
        if mfl_path is None and _contains_mfl_data(child):
            mfl_path = child
        if ultrasound_path is None and _contains_ultrasound_data(child):
            ultrasound_path = child
    if mfl_path is None or ultrasound_path is None:
        return None
    return mfl_path.resolve(), ultrasound_path.resolve()


def _find_dataset_config(root: Path) -> Optional[Path]:
    for candidate in (
        root / "软件" / "config.json",
        root / "software" / "config.json",
        root / "config.json",
    ):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _guess_dataset_root(config_path: Path) -> Path:
    config_dir = Path(config_path).resolve().parent
    if config_dir.name.lower() in {"软件", "software"}:
        return config_dir.parent
    config = _read_json_config(Path(config_path).resolve())
    raw_mfl_path = config.get("mfl_data_dir")
    if raw_mfl_path:
        mfl_path = Path(str(raw_mfl_path))
        if not mfl_path.is_absolute():
            mfl_path = config_dir / mfl_path
        mfl_path = mfl_path.resolve()
        for ancestor in (mfl_path, *mfl_path.parents):
            if ancestor.name.lower() == "data":
                return ancestor.parent
    return config_dir


def _prepare_dataset_selection(
    selected_dir: Path,
    fallback_config_path: Path,
) -> tuple[Path, str, Path]:
    """Load or create the persistent config belonging to a selected dataset."""
    selected = selected_dir.expanduser().resolve()
    if not selected.is_dir():
        raise ValueError("所选路径不是文件夹。")

    hinted_dataset: Optional[str] = None
    root = selected
    nested_selected_layout = _find_nested_dataset_layout(selected)
    selected_mfl_path = _find_mfl_data_path(selected)
    selected_is_mfl = selected_mfl_path == selected.resolve()
    selected_name = selected.name.lower()
    explicit_mfl_path: Optional[Path] = None
    explicit_ultrasound_path: Optional[Path] = None
    if nested_selected_layout is not None:
        hinted_dataset = selected.name
        explicit_mfl_path, explicit_ultrasound_path = nested_selected_layout
        if selected.parent.name.lower() == "data":
            root = selected.parent.parent
        else:
            root = selected.parent
    elif selected_mfl_path is not None and selected_mfl_path != selected.resolve():
        # Selected a date package that currently has only the MFL child.
        hinted_dataset = selected.name
        explicit_mfl_path = selected_mfl_path
        if selected.parent.name.lower() == "data":
            root = selected.parent.parent
        else:
            root = selected.parent
    elif selected_name in {"软件", "software"} and (selected / "config.json").is_file():
        root = selected.parent
    elif selected_is_mfl:
        explicit_mfl_path = selected
        if selected.parent.parent.name.lower() == "data":
            # Directly selecting data/<date>/MFL should still use the date as
            # the dataset name and the distribution root as config root.
            hinted_dataset = selected.parent.name
            root = selected.parent.parent.parent
        elif selected.parent.name.lower() == "data":
            hinted_dataset = selected.name
            root = selected.parent.parent
        else:
            hinted_dataset = selected.name
            root = selected.parent
    elif selected_name in {"data", "ultrasound_data", "ultrasound-data"}:
        root = selected.parent

    source_config = _find_dataset_config(root)
    template_path = source_config or fallback_config_path.resolve()
    template = _read_json_config(template_path)
    if not template:
        raise ValueError(f"无法读取配置文件：{template_path}")

    data_root = root / "data"
    configured_dataset = str(template.get("dataset", "")).strip()
    dataset_candidates: list[Path] = []
    if data_root.is_dir():
        try:
            dataset_candidates = sorted(
                (item for item in data_root.iterdir() if item.is_dir()),
                key=lambda item: item.name,
            )
        except OSError:
            dataset_candidates = []

    dataset_name = ""
    for candidate_name in (hinted_dataset, configured_dataset):
        if candidate_name and (data_root / candidate_name).is_dir():
            dataset_name = candidate_name
            break
    if not dataset_name:
        for candidate in dataset_candidates:
            if (
                _contains_mfl_data(candidate)
                or _find_nested_dataset_layout(candidate) is not None
            ):
                dataset_name = candidate.name
                break
    if not dataset_name and hinted_dataset:
        dataset_name = hinted_dataset
    if not dataset_name:
        dataset_name = configured_dataset or "20251221"

    nested_dataset_layout = _find_nested_dataset_layout(data_root / dataset_name)
    if explicit_mfl_path is not None:
        mfl_path = explicit_mfl_path
    elif selected_is_mfl:
        mfl_path = selected
    elif nested_dataset_layout is not None:
        mfl_path = nested_dataset_layout[0]
    else:
        mfl_path = data_root / dataset_name
        if not mfl_path.is_dir():
            configured_mfl = template.get("mfl_data_dir")
            if configured_mfl:
                candidate = Path(str(configured_mfl))
                if not candidate.is_absolute():
                    candidate = template_path.parent / candidate
                if candidate.is_dir():
                    mfl_path = candidate.resolve()
    if not _contains_mfl_data(mfl_path):
        raise ValueError(
            "所选文件夹中没有找到漏磁 DAT 数据。请选择“portable-release”目录，"
            "或直接选择 data\\日期目录（例如 data\\20251221）。"
        )

    dataset_package = data_root / dataset_name
    if not dataset_package.is_dir() and nested_selected_layout is not None:
        dataset_package = selected

    dataset_config_path = dataset_package / "config.json"
    persisted_config: dict[str, Any] = {}
    if dataset_config_path.is_file():
        try:
            saved_value = json.loads(
                dataset_config_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Dataset config is present but cannot be read: {dataset_config_path}"
            ) from exc
        if not isinstance(saved_value, dict):
            raise ValueError(
                f"Dataset config must contain a JSON object: {dataset_config_path}"
            )
        persisted_config = saved_value

    annotation_path = dataset_package / "annotations"
    if not annotation_path.is_dir():
        annotation_path = root / "annotations"
    if not annotation_path.is_dir():
        annotation_path = root / "annotation"
    if not annotation_path.exists():
        annotation_path = root / "annotations"

    if explicit_ultrasound_path is not None:
        ultrasound_root = explicit_ultrasound_path
    elif nested_dataset_layout is not None:
        ultrasound_root = nested_dataset_layout[1]
    elif explicit_mfl_path is not None:
        # Do not reuse the default dataset's ultrasound cache when a selected
        # date has no ultrasound folder yet.  ReplayService will keep MFL
        # usable and report ultrasound as unavailable.
        ultrasound_root = explicit_mfl_path.parent / "超声"
    else:
        ultrasound_root = root / "ultrasound_data" / dataset_name
    ultrasound_data_path = ultrasound_root / "data"
    if not ultrasound_data_path.exists() and (ultrasound_root / "manifest.json").exists():
        ultrasound_data_path = ultrasound_root
    ultrasound_csv_path = ultrasound_root / "csv"

    model_dir = dataset_package / "model_results"
    if not model_dir.is_dir() and dataset_name == configured_dataset:
        model_dir = root / "model_results"
    model_path: Optional[Path] = None
    raw_model_path = template.get("model_result_path")
    preferred_model_name = Path(str(raw_model_path)).name if raw_model_path else ""
    if model_dir.is_dir():
        if preferred_model_name and (model_dir / preferred_model_name).is_file():
            model_path = (model_dir / preferred_model_name).resolve()
        else:
            for candidate_name in (
                "candidate_events.csv",
                "crack_coherence_preview_v3.csv",
            ):
                candidate = model_dir / candidate_name
                if candidate.is_file():
                    model_path = candidate.resolve()
                    break
            if model_path is None:
                try:
                    model_path = next(
                        (item.resolve() for item in sorted(model_dir.glob("*.csv"))),
                        None,
                    )
                except OSError:
                    model_path = None

    if persisted_config:
        saved_mfl_path = _resolve_config_path(
            persisted_config.get("mfl_data_dir"),
            dataset_config_path.parent,
        )
        if saved_mfl_path is not None and _contains_mfl_data(saved_mfl_path):
            mfl_path = saved_mfl_path

        saved_annotation_path = _resolve_config_path(
            persisted_config.get("annotation_dir"),
            dataset_config_path.parent,
        )
        if saved_annotation_path is not None and saved_annotation_path.is_dir():
            annotation_path = saved_annotation_path

        saved_ultrasound_path = _resolve_config_path(
            persisted_config.get("ultrasound_data_dir"),
            dataset_config_path.parent,
        )
        if saved_ultrasound_path is not None and saved_ultrasound_path.exists():
            ultrasound_data_path = saved_ultrasound_path

        saved_csv_path = _resolve_config_path(
            persisted_config.get("ultrasound_csv_dir"),
            dataset_config_path.parent,
        )
        if saved_csv_path is not None and saved_csv_path.is_dir():
            ultrasound_csv_path = saved_csv_path

        saved_model_path = _resolve_config_path(
            persisted_config.get("model_result_path"),
            dataset_config_path.parent,
        )
        if saved_model_path is not None and saved_model_path.is_file():
            model_path = saved_model_path

    config = _merge_config_dicts(template, persisted_config)
    config["dataset"] = dataset_name
    config["mfl_data_dir"] = _portable_config_path(
        mfl_path,
        dataset_config_path.parent,
    )
    config["annotation_dir"] = _portable_config_path(
        annotation_path,
        dataset_config_path.parent,
    )
    config["ultrasound_data_dir"] = _portable_config_path(
        ultrasound_data_path,
        dataset_config_path.parent,
    )
    if ultrasound_csv_path.is_dir():
        config["ultrasound_csv_dir"] = _portable_config_path(
            ultrasound_csv_path,
            dataset_config_path.parent,
        )
    else:
        config.pop("ultrasound_csv_dir", None)
    if model_path is not None:
        config["model_result_path"] = _portable_config_path(
            model_path,
            dataset_config_path.parent,
        )
    else:
        config.pop("model_result_path", None)

    if not persisted_config and dataset_name != configured_dataset:
        alignment = config.get("alignment")
        if not isinstance(alignment, dict):
            alignment = {}
        alignment["ultrasound_offset_m"] = 0.0
        config["alignment"] = alignment

    _write_json_config(dataset_config_path, config)
    return dataset_config_path.resolve(), dataset_name, root.resolve()
MFL_Y_LIMIT_DEFAULT = 0.5
MFL_Y_LIMIT_MIN = 0.08
MFL_PLOT_HEIGHT_FACTOR = 1.4
MFL_X_COLOR = "#3D6CFF"
MFL_Z_COLOR = "#24D957"
CURSOR_COLOR = "#FF3B30"
DEPTH_MAX_MM = 176.0
US_ALIGNMENT_OFFSET_LIMIT_M = 5.0
US_ALIGNMENT_FINE_STEP_M = 0.001
US_ALIGNMENT_COARSE_STEP_M = 0.01
ANNOTATION_COLORS = {
    "weld": "#FFB000",
    "diagonal_crack": "#FF3B30",
    "vibration_interference": "#B45CFF",
    "suspected_diagonal_crack": "#FF66B3",
    "unknown_anomaly": "#21B8C8",
}
ANNOTATION_MIN_WIDTH_M = 0.002
MFL_THEMES = {
    "dark": {
        "background": "#07111B",
        "row_alt": "#0A1824",
        "row_border": "#2D4654",
        "zero_line": "#74838A",
        "separator": "#385462",
        "annotation": "#78909E",
        "left_label": "#5BD7E5",
        "right_label": "#FFAD68",
        "legend_background": "#122532",
        "legend_border": "#78909E",
        "trace_x": MFL_X_COLOR,
        "trace_z": MFL_Z_COLOR,
        "marker_outline": "#061018",
        "segment": "#D2A15B",
    },
    "light": {
        "background": "#FFFFFF",
        "row_alt": "#F3F6F8",
        "row_border": "#8B979E",
        "zero_line": "#5C6870",
        "separator": "#7D8A91",
        "annotation": "#4B5961",
        "left_label": "#007C91",
        "right_label": "#B85A00",
        "legend_background": "#F1F4F6",
        "legend_border": "#7A878E",
        "trace_x": "#1B55DF",
        "trace_z": "#079737",
        "marker_outline": "#24313A",
        "segment": "#A06B21",
    },
}
GATE_COLORS = (
    "#35F2FF",
    "#E783FF",
    "#FF9F43",
    "#FF4F87",
    "#55F58A",
    "#FFE45C",
    "#719BFF",
    "#F08CFF",
)

# GateNo 1〜22的业务编号以《钢轨探伤车超声检测数据格式规范 V1.0》
# 修订版表 21 为准。图形是原超声项目根据闸门图样重绘的矢量近似，但与业务编号一起按修订表重新对齐。
# The native viewer draws these paths directly so the legend and plot always
# use exactly the same marker geometry.
GATE_STYLE = {
    1: ("long", "#A040C0", -18),
    2: ("long", "#FFFF10", 18),
    3: ("long", "#D02040", 12),
    4: ("long", "#E02040", -45),
    5: ("bars", "#F8FAFC", 0),
    6: ("bars", "#F8FAFC", 0),
    7: ("long", "#00A0C0", -18),
    8: ("long", "#20C040", 18),
    9: ("long", "#F8FAFC", 12),
    10: ("long", "#F8FAFC", -45),
    11: ("bars", "#4040C0", 0),
    12: ("bars", "#80A0C0", 0),
    13: ("capsule", "#FFFF10", -55),
    14: ("capsule", "#A040C0", -55),
    15: ("capsule", "#FFFF10", 0),
    16: ("capsule", "#C05010", -55),
    17: ("capsule", "#E0A0C0", -55),
    18: ("capsule", "#70C040", 0),
    19: ("capsule", "#E02040", -45),
    20: ("capsule", "#F8FAFC", -45),
    21: ("side", "#00A0C0", 10),
    22: ("side", "#F8FAFC", -10),
}
# These nine reference symbols are horizontally mirrored relative to the
# initial vector reconstruction. Keep the correction explicit so the plot and
# the legend preview always use the same orientation.
GATE_HORIZONTAL_FLIP = frozenset({2, 3, 4, 7, 13, 16, 20, 21, 22})

GATE_NAMES = {
    1: "\u524d\u5411\u5916\u4fa7 70\u00b0",
    2: "\u524d\u5411\u4e2d\u5fc3 70\u00b0",
    3: "\u524d\u5411\u5185\u4fa7 70\u00b0",
    4: "\u524d\u5411 45\u00b0",
    5: "\u524d\u5411 0\u00b0\u76d1\u89c6",
    6: "\u524d\u5411 0\u00b0\u5e95\u6ce2\u7f3a\u5931",
    7: "\u540e\u5411\u5916\u4fa7 70\u00b0",
    8: "\u540e\u5411\u4e2d\u5fc3 70\u00b0",
    9: "\u540e\u5411\u5185\u4fa7 70\u00b0",
    10: "\u540e\u5411 45\u00b0",
    11: "\u540e\u5411 0\u00b0\u76d1\u89c6",
    12: "\u540e\u5411 0\u00b0\u5e95\u6ce2\u7f3a\u5931",
    13: "\u524d\u5411\u5185\u4fa7\u504f\u659c 70\u00b0",
    14: "\u540e\u5411\u5185\u4fa7\u504f\u659c 70\u00b0",
    15: "\u5185\u504f\u659c\u5bf9\u5e94\u8f6e 0\u00b0\u5e95\u6ce2\u7f3a\u5931",
    16: "\u524d\u5411\u5916\u4fa7\u504f\u659c 70\u00b0",
    17: "\u540e\u5411\u5916\u4fa7\u504f\u659c 70\u00b0",
    18: "\u5916\u504f\u659c\u5bf9\u5e94\u8f6e 0\u00b0\u5e95\u6ce2\u7f3a\u5931",
    19: "\u524d\u5411 45\u00b0\u8f68\u5e95",
    20: "\u540e\u5411 45\u00b0\u8f68\u5e95",
    21: "\u524d\u5411\u4fa7\u6253",
    22: "\u540e\u5411\u4fa7\u6253",
}
GATE_SHAPE_NAMES = {
    "long": "\u957f\u6846",
    "bars": "\u6a2a\u6761\u6846",
    "capsule": "\u80f6\u56ca\u6846",
    "side": "\u4fa7\u6253\u6846",
    "fallback": "\u5706\u6846",
}
GATE_COLOR_NAMES = {
    "#A040C0": "\u7d2b\u8272",
    "#FFFF10": "\u9ec4\u8272",
    "#D02040": "\u7ea2\u8272",
    "#E02040": "\u6df1\u7ea2\u8272",
    "#F8FAFC": "\u767d\u8272",
    "#00A0C0": "\u9752\u8272",
    "#20C040": "\u7eff\u8272",
    "#4040C0": "\u84dd\u8272",
    "#80A0C0": "\u6d45\u84dd\u8272",
    "#C05010": "\u6a59\u8272",
    "#E0A0C0": "\u7c89\u8272",
    "#70C040": "\u4eae\u7eff\u8272",
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def visible_index_range(
    x_values: list[Any],
    x_start: float,
    x_end: float,
) -> tuple[int, int]:
    """Return a padded visible slice for an ascending x-coordinate list."""
    if not x_values:
        return 0, 0
    length = len(x_values)
    try:
        ascending = float(x_values[0]) <= float(x_values[-1])
    except (TypeError, ValueError):
        return 0, length
    if not ascending:
        # The current dataset is ascending. Keep a correctness-first fallback
        # for a future reverse-direction dataset.
        return 0, length
    start = max(0, bisect_left(x_values, float(x_start)) - 1)
    stop = min(length, bisect_right(x_values, float(x_end)) + 1)
    return start, stop


def format_distance(value: float) -> str:
    if not math.isfinite(float(value)):
        return "—"
    value = float(value)
    absolute = abs(value)
    if absolute >= 1000:
        return f"{value / 1000:.1f} km" if absolute >= 10000 else f"{value / 1000:.2f} km"
    return f"{value:.2f} m" if absolute < 10 else f"{value:.1f} m"


def format_count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def gate_color(gate: int) -> QColor:
    return QColor(GATE_COLORS[abs(int(gate)) % len(GATE_COLORS)])


def gate_style(gate: int) -> tuple[str, str, float]:
    return GATE_STYLE.get(int(gate), ("fallback", GATE_COLORS[abs(int(gate)) % len(GATE_COLORS)], 0.0))


def gate_display_color(gate: int) -> QColor:
    color = QColor(gate_style(gate)[1])
    if color.name().lower() in {"#ffffff", "#f8fafc"}:
        return QColor("#FFFFFF")
    return color


def gate_marker_path(shape: str, size: float) -> QPainterPath:
    """Build one of the four vector Gate outlines used by the US viewer."""
    size = max(6.0, float(size))
    path = QPainterPath()
    if shape == "long":
        path.addRoundedRect(QRectF(-0.28 * size, -0.50 * size, 0.56 * size, size), 0.10 * size, 0.10 * size)
    elif shape == "bars":
        path.addRoundedRect(QRectF(-0.50 * size, -0.30 * size, size, 0.60 * size), 0.10 * size, 0.10 * size)
    elif shape == "capsule":
        path.moveTo(-0.50 * size, 0.0)
        path.lineTo(-0.30 * size, -0.32 * size)
        path.lineTo(0.30 * size, -0.32 * size)
        path.lineTo(0.50 * size, 0.0)
        path.lineTo(0.30 * size, 0.32 * size)
        path.lineTo(-0.30 * size, 0.32 * size)
        path.closeSubpath()
    elif shape == "side":
        path.moveTo(-0.50 * size, -0.24 * size)
        path.lineTo(0.38 * size, -0.33 * size)
        path.lineTo(0.50 * size, 0.24 * size)
        path.lineTo(-0.38 * size, 0.33 * size)
        path.closeSubpath()
    else:
        path.addEllipse(QRectF(-0.42 * size, -0.42 * size, 0.84 * size, 0.84 * size))
    return path


def draw_gate_marker(
    painter: QPainter,
    x: float,
    y: float,
    gate: int,
    size: float,
) -> None:
    """Draw a legible, directional Gate marker with a dark halo."""
    shape, _color, angle = gate_style(gate)
    size = max(6.0, float(size))
    path = gate_marker_path(shape, size)
    color = gate_display_color(gate)
    fill = QColor(color)
    fill.setAlpha(205)
    outer_pen = QPen(QColor("#02080D"), max(1.4, size * 0.17), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    inner_pen = QPen(color, max(0.9, size * 0.095), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)

    painter.save()
    painter.translate(float(x), float(y))
    if int(gate) in GATE_HORIZONTAL_FLIP:
        painter.scale(-1.0, 1.0)
    # QPainter's y axis points down; negate the Matplotlib/SVG angle to keep
    # the orientation consistent with the established ultrasound viewer.
    painter.rotate(-float(angle))
    painter.setPen(outer_pen)
    painter.setBrush(QColor("#02080D"))
    painter.drawPath(path)
    painter.setPen(inner_pen)
    painter.setBrush(fill)
    painter.drawPath(path)

    def draw_inner_lines() -> None:
        if shape == "long":
            for x_offset in (-0.11, 0.11):
                painter.drawLine(
                    QPointF(x_offset * size, -0.42 * size),
                    QPointF(x_offset * size, 0.42 * size),
                )
        elif shape == "bars":
            for y_offset in (-0.15, 0.0, 0.15):
                painter.drawLine(
                    QPointF(-0.40 * size, y_offset * size),
                    QPointF(0.40 * size, y_offset * size),
                )
        elif shape == "capsule":
            for x_offset in (-0.19, 0.0, 0.19):
                painter.drawLine(
                    QPointF(x_offset * size, -0.27 * size),
                    QPointF(x_offset * size, 0.27 * size),
                )
        elif shape == "side":
            painter.drawLine(
                QPointF(-0.38 * size, -0.09 * size),
                QPointF(0.38 * size, -0.18 * size),
            )
            painter.drawLine(
                QPointF(-0.34 * size, 0.10 * size),
                QPointF(0.40 * size, 0.01 * size),
            )
        else:
            painter.drawLine(QPointF(-0.26 * size, 0.0), QPointF(0.26 * size, 0.0))
            painter.drawLine(QPointF(0.0, -0.26 * size), QPointF(0.0, 0.26 * size))

    painter.setPen(outer_pen)
    draw_inner_lines()
    painter.setPen(inner_pen)
    draw_inner_lines()
    painter.restore()


class GateMarkerPreview(QWidget):
    """Show one Gate marker using the same painter as the ultrasound plot."""

    def __init__(self, gate: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.gate = int(gate)
        self.setFixedSize(74, 40)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#02080D"))
        painter.setPen(QPen(QColor("#28404E"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        draw_gate_marker(painter, self.width() / 2.0, self.height() / 2.0, self.gate, 20.0)
        painter.end()


class GateLegendDialog(QDialog):
    """A readable GateNo-to-symbol reference for the native replay window."""

    def __init__(self, owner: "ReplayWindow") -> None:
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("\u8d85\u58f0 Gate \u6807\u8bb0\u5bf9\u7167\u8868")
        self.setMinimumSize(820, 520)
        self.resize(940, 700)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(8)

        intro = QLabel(
            "\u56fe\u6807\u9884\u89c8\u4e0e\u4e0a\u65b9\u8d85\u58f0\u56fe\u4f7f\u7528\u540c\u4e00\u5957\u7ed8\u5236\u89c4\u5219\uff1b"
            "Track 0 = \u4e0b\u80a1\uff0cTrack 1 = \u4e0a\u80a1\uff0cGateNo \u51b3\u5b9a\u56fe\u6807\u5f62\u72b6\u3001\u989c\u8272\u548c\u65b9\u5411\u3002"
        )
        intro.setWordWrap(True)
        intro.setObjectName("InfoLabel")
        root_layout.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        panel = QWidget()
        grid = QGridLayout(panel)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(3)

        headers = ("\u6807\u8bb0\u9884\u89c8", "GateNo", "\u5bf9\u5e94\u901a\u9053 / \u529f\u80fd", "\u56fe\u5f62 / \u989c\u8272 / \u89d2\u5ea6", "20251221\u8bb0\u5f55")
        for column, title in enumerate(headers):
            label = QLabel(title)
            label.setStyleSheet("color: #9FC0CF; font-weight: 600; padding: 4px;")
            grid.addWidget(label, 0, column)

        present_counts = {
            int(item.get("gate", 0)): int(item.get("count", 0))
            for item in owner.manifest.get("ultrasound", {}).get("gates", [])
        } if owner.manifest else {}

        for row_index, gate in enumerate(sorted(GATE_NAMES), start=1):
            preview = GateMarkerPreview(gate)
            grid.addWidget(preview, row_index, 0, Qt.AlignCenter)

            number_label = QLabel(f"G{gate}")
            number_label.setStyleSheet("color: #E7F0F5; font-weight: 700;")
            grid.addWidget(number_label, row_index, 1, Qt.AlignVCenter)

            name_label = QLabel(GATE_NAMES[gate])
            name_label.setWordWrap(True)
            name_label.setMinimumWidth(210)
            grid.addWidget(name_label, row_index, 2, Qt.AlignVCenter)

            shape, color, angle = gate_style(gate)
            color_key = str(color).upper()
            detail = QLabel(
                f"{GATE_SHAPE_NAMES.get(shape, shape)}  "
                f"{GATE_COLOR_NAMES.get(color_key, color_key)}  "
                f"{angle:+g}\u00b0"
            )
            detail.setStyleSheet(f"color: {gate_display_color(gate).name()};")
            grid.addWidget(detail, row_index, 3, Qt.AlignVCenter)

            if gate in present_counts:
                status = QLabel(format_count(present_counts[gate]) + " \u6761")
                status.setStyleSheet("color: #83DC9D;")
            else:
                status = QLabel("\u672c\u6279\u65e0\u8bb0\u5f55")
                status.setStyleSheet("color: #78909E;")
            grid.addWidget(status, row_index, 4, Qt.AlignVCenter)

        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 1)
        scroll.setWidget(panel)
        root_layout.addWidget(scroll, 1)

        note = QLabel(
            "\u6ce8\u610f\uff1aGate 5 \u5728 CSV \u683c\u5f0f\u8bf4\u660e\u4e2d\u66fe\u8bb0\u4e3a\u201c\u524d\u5411 37\u00b0\u76d1\u89c6\u201d\uff0c\u4e0e\u4fee\u8ba2\u7248\u8868 21 \u6709\u51b2\u7a81\uff1b"
            "\u5f53\u524d\u7a0b\u5e8f\u6309\u4fee\u8ba2\u7248\u8868 21 \u663e\u793a\u4e3a\u201c\u524d\u5411 0\u00b0\u76d1\u89c6\u201d\uff0c\u8be5\u89d2\u5ea6\u4ecd\u9700\u6570\u636e\u63d0\u4f9b\u65b9\u6700\u7ec8\u786e\u8ba4\u3002"
        )
        note.setWordWrap(True)
        note.setObjectName("InfoLabel")
        root_layout.addWidget(note)

        close_button = QPushButton("\u5173\u95ed")
        close_button.clicked.connect(self.close)
        root_layout.addWidget(close_button, 0, Qt.AlignRight)


def format_signal_value(value: float) -> str:
    value = float(value)
    if not math.isfinite(value):
        return "?"
    if abs(value) >= 10:
        return f"{value:.1f}"
    if abs(value) >= 1:
        return f"{value:.2f}"
    return f"{value:.3f}"


def format_cursor_signal_value(value: Any) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{numeric:.3f}" if math.isfinite(numeric) else "—"


def color_with_alpha(color: QColor, alpha: int) -> QColor:
    copied = QColor(color)
    copied.setAlpha(alpha)
    return copied


class DataWorker(QThread):
    ready = pyqtSignal(object)
    window_ready = pyqtSignal(int, object)
    cursor_ready = pyqtSignal(int, object)
    joint_ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)
    joint_failed = pyqtSignal(str)
    joint_progress = pyqtSignal(str)
    ultrasound_joint_ready = pyqtSignal(object)
    ultrasound_joint_failed = pyqtSignal(str)
    ultrasound_joint_progress = pyqtSignal(str)

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.service: Optional[ReplayService] = None
        self._mutex = QMutex()
        self._condition = QWaitCondition()
        self._pending: deque[
            tuple[int, float, float, list[int], Optional[list[int]]]
        ] = deque()
        self._pending_cursor: Optional[tuple[int, float]] = None
        self._joint_search_pending = False
        self._ultrasound_joint_search_pending = False
        self._stopping = False

    def request_window(
        self,
        serial: int,
        x_start: float,
        x_end: float,
        tracks: list[int],
        gates: Optional[list[int]],
        priority: bool = False,
    ) -> None:
        self._mutex.lock()
        request = (serial, x_start, x_end, tracks, gates)
        if priority:
            self._pending.appendleft(request)
        else:
            self._pending.append(request)
        self._condition.wakeAll()
        self._mutex.unlock()

    def request_cursor(self, serial: int, x_value: float) -> None:
        # Only the newest pointer position matters. Coalescing requests keeps
        # rapid mouse movement from building a queue behind window prefetches.
        self._mutex.lock()
        self._pending_cursor = (serial, float(x_value))
        self._condition.wakeAll()
        self._mutex.unlock()

    def request_joint_search(self) -> None:
        """Queue a joint search with priority over stale window requests."""
        self._mutex.lock()
        self._joint_search_pending = True
        self._condition.wakeAll()
        self._mutex.unlock()

    def request_ultrasound_joint_search(self) -> None:
        """Queue an ultrasound joint search with priority over plot requests."""
        self._mutex.lock()
        self._ultrasound_joint_search_pending = True
        self._condition.wakeAll()
        self._mutex.unlock()

    def request_stop(self) -> None:
        self._mutex.lock()
        self._stopping = True
        self._condition.wakeAll()
        self._mutex.unlock()

    def stop(self, timeout: int | None = 4000) -> bool:
        """Request shutdown and wait for the worker when closing the app."""
        self.request_stop()
        if timeout is None:
            return bool(self.wait())
        return bool(self.wait(timeout))

    def run(self) -> None:
        try:
            self.service = ReplayService(
                self.config_path,
                progress_callback=self.progress.emit,
                stop_callback=lambda: self._stopping,
            )
            if self._stopping:
                return
            self.ready.emit(self.service.manifest())
        except DataLoadCancelled:
            return
        except Exception as exc:
            if self._stopping:
                return
            self.failed.emit(f"数据初始化失败：{exc}")
            return

        while True:
            self._mutex.lock()
            while (
                not self._stopping
                and not self._pending
                and self._pending_cursor is None
                and not self._joint_search_pending
                and not self._ultrasound_joint_search_pending
            ):
                self._condition.wait(self._mutex)
            if self._stopping:
                self._mutex.unlock()
                return
            joint_search = self._joint_search_pending
            ultrasound_joint_search = (
                not joint_search and self._ultrasound_joint_search_pending
            )
            if joint_search:
                self._joint_search_pending = False
            elif ultrasound_joint_search:
                self._ultrasound_joint_search_pending = False
            priority_search = joint_search or ultrasound_joint_search
            cursor_request = None if priority_search else self._pending_cursor
            if priority_search:
                self._pending_cursor = None
                request = None
            elif cursor_request is not None:
                self._pending_cursor = None
                request = None
            else:
                request = self._pending.popleft() if self._pending else None
            self._mutex.unlock()

            if joint_search and self.service is not None:
                try:
                    self.joint_ready.emit(
                        self.service.locate_joint(
                            progress_callback=self.joint_progress.emit,
                            stop_callback=lambda: self._stopping,
                        )
                    )
                except DataLoadCancelled:
                    return
                except Exception as exc:
                    if not self._stopping:
                        self.joint_failed.emit(f"接头定位失败：{exc}")
                continue

            if ultrasound_joint_search and self.service is not None:
                try:
                    self.ultrasound_joint_ready.emit(
                        self.service.locate_ultrasound_joint(
                            progress_callback=self.ultrasound_joint_progress.emit,
                            stop_callback=lambda: self._stopping,
                        )
                    )
                except DataLoadCancelled:
                    return
                except Exception as exc:
                    if not self._stopping:
                        self.ultrasound_joint_failed.emit(
                            f"超声接头检测失败：{exc}"
                        )
                continue

            if cursor_request is not None and self.service is not None:
                cursor_serial, cursor_x = cursor_request
                try:
                    self.cursor_ready.emit(
                        cursor_serial,
                        self.service.mfl.sample_at(cursor_x),
                    )
                except Exception as exc:
                    self.failed.emit(f"漏磁游标读取失败：{exc}")
                continue

            if request is None or self.service is None:
                continue

            serial, x_start, x_end, tracks, gates = request
            try:
                payload = self.service.window(
                    x_start,
                    x_end,
                    tracks=tracks,
                    gates=gates,
                    max_us_records=70000,
                )
                self.window_ready.emit(serial, payload)
            except Exception as exc:
                self.failed.emit(f"窗口读取失败：{exc}")


class PlotCanvas(QWidget):
    def __init__(self, owner: "ReplayWindow") -> None:
        super().__init__(owner)
        self.owner = owner
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumWidth(280)

    def geometry_info(self) -> dict[str, float]:
        left = 86.0
        # Reserve a readable right-side gutter for live MFL X/Z values. Both
        # plots use the same geometry so their shared x-axis stays aligned.
        right = 90.0
        top = 16.0
        bottom = 100.0 if self is getattr(self.owner, "mfl_plot", None) else 64.0
        return {
            "left": left,
            "right": right,
            "top": top,
            "bottom": bottom,
            "plot_width": max(1.0, self.width() - left - right),
            "plot_height": max(1.0, self.height() - top - bottom),
        }

    def x_to_pixel(self, x_value: float, geometry: dict[str, float]) -> float:
        x_start, x_end = self.owner.view_range()
        if x_end <= x_start:
            return geometry["left"]
        return geometry["left"] + (
            (float(x_value) - x_start) / (x_end - x_start) * geometry["plot_width"]
        )

    def pixel_to_x(self, pixel_x: float, geometry: dict[str, float]) -> float:
        x_start, x_end = self.owner.view_range()
        return x_start + (
            (float(pixel_x) - geometry["left"]) / geometry["plot_width"]
            * (x_end - x_start)
        )

    def mouseMoveEvent(self, event: Any) -> None:
        geometry = self.geometry_info()
        if geometry["left"] <= event.x() <= geometry["left"] + geometry["plot_width"]:
            x_value = self.pixel_to_x(event.x(), geometry)
            self.owner.set_cursor(x_value)
            if (
                self is getattr(self.owner, "mfl_plot", None)
                and self.owner.annotation_mode
                and self.owner.annotation_drag_start_x is not None
            ):
                self.owner.update_annotation_drag(x_value)
        else:
            self.owner.clear_cursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        geometry = self.geometry_info()
        plot_left = geometry["left"]
        plot_right = plot_left + geometry["plot_width"]
        if (event.button() == Qt.LeftButton and self.owner.measure_mode
                and plot_left <= event.x() <= plot_right
                and geometry["top"] <= event.y() <= geometry["top"] + geometry["plot_height"]):
            self.owner.add_measure_point(self.pixel_to_x(event.x(), geometry))
            event.accept()
            return
        if (
            event.button() == Qt.LeftButton
            and self is getattr(self.owner, "mfl_plot", None)
            and plot_left <= event.x() <= plot_right
        ):
            x_value = self.pixel_to_x(event.x(), geometry)
            saved_annotation = self.owner.annotation_at_x(x_value)
            if saved_annotation is not None:
                self.owner.load_annotation_for_edit(saved_annotation)
                event.accept()
                return
            if self.owner.annotation_mode:
                self.owner.begin_annotation_drag(x_value)
                event.accept()
                return
        if (
            event.button() == Qt.LeftButton
            and not self.owner.annotation_mode
            and not self.owner.measure_mode
            and plot_left <= event.x() <= plot_right
            and geometry["top"] <= event.y() <= geometry["top"] + geometry["plot_height"]
        ):
            model_prediction = self.owner.model_prediction_at_x(
                self.pixel_to_x(event.x(), geometry)
            )
            if model_prediction is not None:
                self.owner.select_model_prediction(model_prediction)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        geometry = self.geometry_info()
        plot_left = geometry["left"]
        plot_right = plot_left + geometry["plot_width"]
        if (
            event.button() == Qt.LeftButton
            and self is getattr(self.owner, "mfl_plot", None)
            and self.owner.annotation_mode
            and self.owner.annotation_drag_start_x is not None
        ):
            pixel_x = clamp(float(event.x()), plot_left, plot_right)
            self.owner.finish_annotation_drag(
                self.pixel_to_x(pixel_x, geometry)
            )
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self.owner.clear_cursor()
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if self.owner.measure_mode:
            event.accept()
            return
        geometry = self.geometry_info()
        if geometry["left"] <= event.x() <= geometry["left"] + geometry["plot_width"]:
            self.owner.center_on(self.pixel_to_x(event.x(), geometry))
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: Any) -> None:
        geometry = self.geometry_info()
        if geometry["left"] <= event.x() <= geometry["left"] + geometry["plot_width"]:
            # Invert the previous mapping so the browsing gesture follows the
            # expected replay direction. Qt reports wheel-down as a negative y
            # delta, so keep the sign conversion explicit here.
            delta = 0.5 if event.angleDelta().y() < 0 else -0.5
            self.owner.move_position(delta)
            event.accept()
            return
        super().wheelEvent(event)


class UltrasoundPlot(PlotCanvas):
    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor("#07111b"))
        geometry = self.geometry_info()
        x_start, x_end = self.owner.view_range()

        self.owner.draw_x_grid(painter, geometry, draw_labels=True)

        tracks = sorted(self.owner.selected_tracks(), reverse=True)
        band_count = max(1, len(tracks))
        track_gap = (
            min(24.0, max(14.0, geometry["plot_height"] * 0.06))
            if band_count > 1
            else 0.0
        )
        band_height = max(
            1.0,
            (geometry["plot_height"] - track_gap * (band_count - 1)) / band_count,
        )

        def band_top_for(index: int) -> float:
            return geometry["top"] + index * (band_height + track_gap)

        painter.save()
        painter.setFont(QFont("Segoe UI", 9))
        for band_index, track in enumerate(tracks):
            band_top = band_top_for(band_index)
            band_bottom = band_top + band_height
            track_base = QColor("#5bd7e5") if track == 0 else QColor("#ffad68")
            painter.fillRect(
                QRectF(geometry["left"], band_top, geometry["plot_width"], band_height),
                color_with_alpha(track_base, 13),
            )
            painter.setPen(QPen(QColor("#294252"), 1))
            painter.drawLine(
                QPointF(geometry["left"], band_bottom),
                QPointF(geometry["left"] + geometry["plot_width"], band_bottom),
            )
            painter.setPen(track_base)
            painter.drawText(
                QRectF(4, band_top + 2, geometry["left"] - 12, 18),
                Qt.AlignRight | Qt.AlignTop,
                "下股" if track == 0 else "上股",
            )

            if band_index < band_count - 1:
                separator_y = band_bottom + track_gap / 2.0
                painter.setPen(QPen(QColor("#385462"), 1, Qt.DashLine))
                painter.drawLine(
                    QPointF(geometry["left"], separator_y),
                    QPointF(geometry["left"] + geometry["plot_width"], separator_y),
                )

            last_depth_label_y = -1e9
            for depth in (0, 48.5, 145.5, 176):
                y = band_top + 22 + depth / DEPTH_MAX_MM * max(1.0, band_height - 34)
                painter.setPen(QPen(QColor("#FFFFFF"), 1, Qt.SolidLine))
                painter.drawLine(
                    QPointF(geometry["left"], y),
                    QPointF(geometry["left"] + geometry["plot_width"], y),
                )
                painter.setPen(QColor("#FFFFFF"))
                if y - last_depth_label_y < painter.fontMetrics().height() + 2:
                    continue
                last_depth_label_y = y
                painter.drawText(
                    QRectF(23, y - 7, 50, 15),
                    Qt.AlignRight | Qt.AlignVCenter,
                    str(depth),
                )
        painter.restore()
        self.owner.draw_model_predictions(painter, geometry, "ultrasound")

        data = self.owner.data.get("ultrasound", {}) if self.owner.data else {}
        count = int(data.get("count", 0) or 0)
        xs = data.get("x", [])
        depths = data.get("depth", [])
        data_tracks = data.get("track", [])
        gates = data.get("gate", [])
        track_bands = {track: index for index, track in enumerate(tracks)}

        marker_key = (id(self.owner.data), self.owner.view_range(),
                      self.width(), self.height(), self.devicePixelRatioF(),
                      tuple(tracks), tuple(self.owner.selected_gates() or []),
                      self.owner.ultrasound_marker_size, self.owner.us_alignment_offset_m)
        if getattr(self, "_marker_layer_key", None) != marker_key:
            ratio = self.devicePixelRatioF()
            layer = QPixmap(round(self.width() * ratio), round(self.height() * ratio))
            layer.setDevicePixelRatio(ratio)
            layer.fill(Qt.transparent)
            marker_painter = QPainter(layer)
            marker_painter.setRenderHint(QPainter.Antialiasing, True)
            if count and tracks:
                # Vector markers are intentionally more expressive than the old
                # 3 px squares.  Cap their count so a wide window remains smooth.
                marker_size = self.owner.ultrasound_marker_size
                us_offset_m = self.owner.us_alignment_offset_m
                # The payload keeps the original synchronized coordinates. Apply
                # the live calibration only while drawing so an offset change is
                # instant and does not invalidate the read-ahead/cache buffers.
                visible_start, visible_stop = visible_index_range(
                    xs,
                    x_start - us_offset_m,
                    x_end - us_offset_m,
                )
                visible_stop = min(visible_stop, count, len(xs))
                visible_count = max(0, visible_stop - visible_start)
                stride = max(1, math.ceil(visible_count / 12000))
                marker_painter.save()
                marker_painter.setClipRect(
                    QRectF(
                        geometry["left"],
                        geometry["top"],
                        geometry["plot_width"],
                        geometry["plot_height"],
                    )
                )
                for index in range(visible_start, visible_stop, stride):
                    track = int(data_tracks[index])
                    band_index = track_bands.get(track)
                    if band_index is None:
                        continue
                    x = self.x_to_pixel(float(xs[index]) + us_offset_m, geometry)
                    band_top = band_top_for(band_index)
                    depth = clamp(float(depths[index]) * 176.0 / 127.0, 0.0, 176.0)
                    y = band_top + 22 + depth / DEPTH_MAX_MM * max(1.0, band_height - 34)
                    gate = int(gates[index]) if index < len(gates) else 0
                    draw_gate_marker(marker_painter, x, y, gate, marker_size)
                marker_painter.restore()

            marker_painter.end()
            self._marker_layer = layer
            self._marker_layer_key = marker_key
            self._marker_layer_data = self.owner.data
        painter.drawPixmap(0, 0, self._marker_layer)

        if not count or not tracks:
            painter.setPen(QColor("#78909e"))
            painter.drawText(
                QRectF(
                    geometry["left"],
                    geometry["top"],
                    geometry["plot_width"],
                    geometry["plot_height"],
                ),
                Qt.AlignCenter,
                painter.fontMetrics().elidedText("当前窗口没有符合筛选条件的超声事件", Qt.ElideRight, int(geometry["plot_width"])),
            )

        self.owner.draw_cursor(painter, geometry)
        self.owner.draw_measurement(painter, geometry)
        painter.end()


class MflPlot(PlotCanvas):
    def paintEvent_legacy(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.fillRect(self.rect(), QColor("#07111b"))
        geometry = self.geometry_info()
        self.owner.draw_x_grid(painter, geometry, draw_labels=True)

        rows = self.owner.data.get("mfl", {}).get("rows", []) if self.owner.data else []
        row_count = 16
        row_height = geometry["plot_height"] / row_count

        painter.save()
        painter.setFont(QFont("Segoe UI", 8))
        for row_index in range(row_count):
            top = geometry["top"] + row_index * row_height
            bottom = top + row_height
            middle = (top + bottom) / 2.0
            if row_index % 2:
                painter.fillRect(
                    QRectF(geometry["left"], top, geometry["plot_width"], row_height),
                    QColor("#0a1824"),
                )
            painter.setPen(QPen(QColor("#1d3342"), 1))
            painter.drawLine(
                QPointF(geometry["left"], bottom),
                QPointF(geometry["left"] + geometry["plot_width"], bottom),
            )
            painter.setPen(QPen(QColor("#223c4c"), 1))
            painter.drawLine(
                QPointF(geometry["left"], middle),
                QPointF(geometry["left"] + geometry["plot_width"], middle),
            )
            for ratio in (0.25, 0.75):
                y = top + ratio * row_height
                painter.setPen(QPen(QColor("#152a38"), 1))
                painter.drawLine(
                    QPointF(geometry["left"], y),
                    QPointF(geometry["left"] + geometry["plot_width"], y),
                )

            if row_index < len(rows):
                row = rows[row_index]
                side = str(row.get("side", "left")).upper()
                channel = int(row.get("channel", row_index % 8 + 1))
            else:
                side = "LEFT" if row_index < 8 else "RIGHT"
                channel = row_index % 8 + 1
            y_limit = (
                self.owner.mfl_y_limits[row_index]
                if row_index < len(self.owner.mfl_y_limits)
                else MFL_Y_LIMIT_DEFAULT
            )
            label_color = QColor("#5bd7e5") if side == "LEFT" else QColor("#ffad68")
            painter.setPen(label_color)
            painter.drawText(
                QRectF(3, top, geometry["left"] - 12, row_height),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{'L' if side == 'LEFT' else 'R'} {channel}",
            )

            painter.setPen(QColor("#78909e"))
            painter.drawText(
                QRectF(47, top + 2, 35, 13),
                Qt.AlignLeft | Qt.AlignTop,
                f"+{format_signal_value(y_limit)}",
            )
            painter.drawText(
                QRectF(47, bottom - 15, 35, 13),
                Qt.AlignLeft | Qt.AlignBottom,
                f"-{format_signal_value(y_limit)}",
            )

        painter.restore()

        has_data = False
        painter.save()
        painter.setClipRect(
            QRectF(
                geometry["left"],
                geometry["top"],
                geometry["plot_width"],
                geometry["plot_height"],
            )
        )
        for row_index, row in enumerate(rows[:row_count]):
            top = geometry["top"] + row_index * row_height
            middle = top + row_height / 2.0
            y_limit = (
                self.owner.mfl_y_limits[row_index]
                if row_index < len(self.owner.mfl_y_limits)
                else MFL_Y_LIMIT_DEFAULT
            )
            scale = max(1.0, row_height / 2.0 - 4.0) / max(MFL_Y_LIMIT_MIN, y_limit)
            for segment in row.get("segments", []):
                xs = segment.get("x", [])
                x_values = segment.get("xValue", [])
                z_values = segment.get("zValue", [])
                if xs:
                    has_data = True
                self.draw_trace(
                    painter,
                    geometry,
                    xs,
                    x_values,
                    middle,
                    scale,
                    y_limit,
                    QColor("#5bd7e5"),
                )
                self.draw_trace(
                    painter,
                    geometry,
                    xs,
                    z_values,
                    middle,
                    scale,
                    y_limit,
                    QColor("#ffad68"),
                )
        painter.restore()

        if not has_data:
            painter.setPen(QColor("#78909e"))
            painter.drawText(
                QRectF(
                    geometry["left"],
                    geometry["top"],
                    geometry["plot_width"],
                    geometry["plot_height"],
                ),
                Qt.AlignCenter,
                painter.fontMetrics().elidedText("当前窗口没有可用的漏磁数据", Qt.ElideRight, int(geometry["plot_width"])),
            )

        self.owner.draw_segment_markers(painter, geometry, rows)
        self.owner.draw_cursor(painter, geometry)
        painter.end()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        theme = MFL_THEMES.get(self.owner.mfl_background_mode, MFL_THEMES["dark"])
        painter.fillRect(self.rect(), QColor(theme["background"]))
        geometry = self.geometry_info()
        self.owner.draw_x_grid(painter, geometry, draw_labels=True)
        rows = self.owner.data.get("mfl", {}).get("rows", []) if self.owner.data else []
        row_count = 16
        channels_per_side = 8
        block_gap = min(24.0, max(16.0, geometry["plot_height"] * 0.06))
        block_height = max(1.0, (geometry["plot_height"] - block_gap) / 2.0)
        row_height = block_height / channels_per_side
        plot_right = geometry["left"] + geometry["plot_width"]
        scale_left = plot_right + 7.0
        scale_width = max(10.0, geometry["right"] - 10.0)
        cursor_sample = self.owner.cursor_sample if self.owner.cursor_x is not None else None
        cursor_rows = (
            cursor_sample.get("rows", [])
            if cursor_sample and cursor_sample.get("available")
            else []
        )
        def row_top(row_index: int) -> float:
            if row_index < channels_per_side:
                return geometry["top"] + row_index * row_height
            return (
                geometry["top"]
                + block_height
                + block_gap
                + (row_index - channels_per_side) * row_height
            )
        painter.save()
        painter.setFont(QFont("Segoe UI", 8))
        middle_pen = QPen(QColor(theme["zero_line"]), 1.0, Qt.CustomDashLine)
        middle_pen.setDashPattern([6.0, 4.0])
        for row_index in range(row_count):
            top = row_top(row_index)
            bottom = top + row_height
            middle = (top + bottom) / 2.0
            if row_index % 2:
                painter.fillRect(
                    QRectF(geometry["left"], top, geometry["plot_width"], row_height),
                    QColor(theme["row_alt"]),
                )
            painter.setPen(QPen(QColor(theme["row_border"]), 1))
            painter.drawLine(
                QPointF(geometry["left"], bottom),
                QPointF(plot_right, bottom),
            )
            painter.setPen(middle_pen)
            painter.drawLine(
                QPointF(geometry["left"], middle),
                QPointF(plot_right, middle),
            )
            if row_index < len(rows):
                row = rows[row_index]
                side = str(row.get("side", "left")).upper()
                channel = int(row.get("channel", row_index % channels_per_side + 1))
            else:
                side = "LEFT" if row_index < channels_per_side else "RIGHT"
                channel = row_index % channels_per_side + 1
            side_text = "\u5de6" if side == "LEFT" else "\u53f3"
            label_color = QColor(
                theme["left_label"] if side == "LEFT" else theme["right_label"]
            )
            painter.setPen(label_color)
            painter.drawText(
                QRectF(3, top, geometry["left"] - 10, row_height),
                Qt.AlignRight | Qt.AlignVCenter,
                f"({side_text})\u901a\u9053{channel}",
            )
            if row_index < len(cursor_rows):
                sample_row = cursor_rows[row_index]
                half_height = row_height / 2.0
                painter.setPen(QColor(theme["trace_x"]))
                painter.drawText(
                    QRectF(scale_left, top, scale_width, half_height),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    "X: " + format_cursor_signal_value(sample_row.get("xValue")),
                )
                painter.setPen(QColor(theme["trace_z"]))
                painter.drawText(
                    QRectF(scale_left, top + half_height, scale_width, half_height),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    "Z: " + format_cursor_signal_value(sample_row.get("zValue")),
                )
        # Separate the two physical sides without consuming a data row.
        separator_y = geometry["top"] + block_height + block_gap / 2.0
        painter.setPen(QPen(QColor(theme["separator"]), 1))
        painter.drawLine(
            QPointF(geometry["left"], separator_y),
            QPointF(plot_right, separator_y),
        )
        painter.setPen(QColor(theme["annotation"]))
        painter.drawText(
            QRectF(geometry["left"] + 5, separator_y - 8, 70, 16),
            Qt.AlignLeft | Qt.AlignVCenter,
            "LEFT / RIGHT",
        )
        painter.setPen(QPen(QColor(theme["row_border"]), 1))
        painter.drawRect(
            QRectF(geometry["left"], geometry["top"], geometry["plot_width"], block_height)
        )
        painter.drawRect(
            QRectF(
                geometry["left"],
                geometry["top"] + block_height + block_gap,
                geometry["plot_width"],
                block_height,
            )
        )
        painter.restore()
        # Keep the X/Z legend in the upper-right, outside the data rows.
        painter.save()
        legend_y = max(1.0, geometry["top"] - 15.0)
        legend_x = plot_right - 54.0
        for offset, label, color in (
            (0.0, "X", QColor(theme["trace_x"])),
            (27.0, "Z", QColor(theme["trace_z"])),
        ):
            painter.setPen(QPen(QColor(theme["legend_border"]), 1))
            painter.setBrush(QColor(theme["legend_background"]))
            painter.drawRect(QRectF(legend_x + offset, legend_y, 24, 16))
            painter.setPen(color)
            painter.drawText(
                QRectF(legend_x + offset, legend_y + 1, 24, 14),
                Qt.AlignCenter,
                label,
            )
        painter.restore()
        has_data = False
        self.owner.draw_model_predictions(painter, geometry, "mfl")
        self.owner.draw_saved_annotations(painter, geometry)
        painter.save()
        painter.setClipRect(
            QRectF(
                geometry["left"],
                geometry["top"],
                geometry["plot_width"],
                geometry["plot_height"],
            )
        )
        trace_key = (id(self.owner.data), self.owner.view_range(),
                     self.width(), self.height(), self.devicePixelRatioF(),
                     tuple(self.owner.mfl_y_limits), self.owner.mfl_background_mode)
        if getattr(self, "_trace_layer_key", None) != trace_key:
            # Cache the rendering of ALL raw points, never a reduced signal.
            ratio = self.devicePixelRatioF()
            layer = QPixmap(round(self.width() * ratio), round(self.height() * ratio))
            layer.setDevicePixelRatio(ratio)
            layer.fill(Qt.transparent)
            trace_painter = QPainter(layer)
            trace_painter.setRenderHint(QPainter.Antialiasing, False)
            for row_index, row in enumerate(rows[:row_count]):
                top = row_top(row_index)
                middle = top + row_height / 2.0
                y_limit = (
                    self.owner.mfl_y_limits[row_index]
                    if row_index < len(self.owner.mfl_y_limits)
                    else MFL_Y_LIMIT_DEFAULT
                )
                scale = max(1.0, row_height / 2.0 - 4.0) / max(MFL_Y_LIMIT_MIN, y_limit)
                for segment in row.get("segments", []):
                    xs = segment.get("x", [])
                    x_values = segment.get("xValue", [])
                    z_values = segment.get("zValue", [])
                    if xs:
                        has_data = True
                    self.draw_trace(
                        trace_painter,
                        geometry,
                        xs,
                        x_values,
                        middle,
                        scale,
                        QColor(theme["trace_x"]),
                    )
                    self.draw_trace(
                        trace_painter,
                        geometry,
                        xs,
                        z_values,
                        middle,
                        scale,
                        QColor(theme["trace_z"]),
                    )
            trace_painter.end()
            self._trace_layer = layer
            self._trace_layer_key = trace_key
            self._trace_layer_data = self.owner.data
            self._trace_layer_has_data = has_data
        has_data = self._trace_layer_has_data
        painter.drawPixmap(0, 0, self._trace_layer)
        painter.restore()
        self.owner.draw_annotation_selection(painter, geometry)
        self.owner.draw_annotation_dimensions(painter, geometry)
        self.owner.draw_measurement(painter, geometry)
        if not has_data:
            painter.setPen(QColor(theme["annotation"]))
            painter.drawText(
                QRectF(
                    geometry["left"],
                    geometry["top"],
                    geometry["plot_width"],
                    geometry["plot_height"],
                ),
                Qt.AlignCenter,
                "\u5f53\u524d\u7a97\u53e3\u6ca1\u6709\u53ef\u7528\u7684\u6f0f\u78c1\u6570\u636e",
            )
        self.owner.draw_segment_markers(painter, geometry, rows)
        self.owner.draw_cursor(painter, geometry)
        self.draw_cursor_intersections(
            painter,
            geometry,
            cursor_rows,
            row_height,
            block_height,
            block_gap,
        )
        painter.end()

    def draw_cursor_intersections(
        self,
        painter: QPainter,
        geometry: dict[str, float],
        cursor_rows: list[dict[str, Any]],
        row_height: float,
        block_height: float,
        block_gap: float,
    ) -> None:
        """Mark the X/Z values where the MFL traces meet the cursor line."""
        if self.owner.cursor_x is None or not cursor_rows:
            return

        theme = MFL_THEMES.get(self.owner.mfl_background_mode, MFL_THEMES["dark"])
        marker_x = self.x_to_pixel(self.owner.cursor_x, geometry)
        plot_top = geometry["top"]
        plot_bottom = self.height() - geometry["bottom"]
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setClipRect(
            QRectF(
                geometry["left"],
                plot_top,
                geometry["plot_width"],
                geometry["plot_height"],
            )
        )

        for row_index, sample_row in enumerate(cursor_rows[:16]):
            if row_index < 8:
                top = plot_top + row_index * row_height
            else:
                top = (
                    plot_top
                    + block_height
                    + block_gap
                    + (row_index - 8) * row_height
                )
            middle = top + row_height / 2.0
            y_limit = (
                self.owner.mfl_y_limits[row_index]
                if row_index < len(self.owner.mfl_y_limits)
                else MFL_Y_LIMIT_DEFAULT
            )
            scale = max(1.0, row_height / 2.0 - 4.0) / max(MFL_Y_LIMIT_MIN, y_limit)
            try:
                x_value = float(sample_row.get("xValue"))
                z_value = float(sample_row.get("zValue"))
            except (TypeError, ValueError):
                continue

            x_y = middle - x_value * scale
            if math.isfinite(x_y) and plot_top <= x_y <= plot_bottom:
                painter.setPen(QPen(QColor(theme["marker_outline"]), 1.4))
                painter.setBrush(QColor(theme["trace_x"]))
                painter.drawEllipse(QPointF(marker_x, x_y), 4.0, 4.0)

            z_y = middle - z_value * scale
            if math.isfinite(z_y) and plot_top <= z_y <= plot_bottom:
                diamond = QPainterPath()
                diamond.moveTo(marker_x, z_y - 4.5)
                diamond.lineTo(marker_x + 4.5, z_y)
                diamond.lineTo(marker_x, z_y + 4.5)
                diamond.lineTo(marker_x - 4.5, z_y)
                diamond.closeSubpath()
                painter.setPen(QPen(QColor(theme["marker_outline"]), 1.4))
                painter.setBrush(QColor(theme["trace_z"]))
                painter.drawPath(diamond)

        painter.restore()

    def draw_trace(
        self,
        painter: QPainter,
        geometry: dict[str, float],
        xs: list[Any],
        values: list[Any],
        middle: float,
        scale: float,
        color: QColor,
    ) -> None:
        if not xs or not values:
            return
        length = min(len(xs), len(values))
        view_start, view_end = self.owner.view_range()
        visible_start, visible_stop = visible_index_range(xs, view_start, view_end)
        visible_stop = min(visible_stop, length)
        visible_length = max(0, visible_stop - visible_start)
        if visible_length <= 0:
            return
        data = self.owner.data
        if getattr(self, "_raw_path_data", None) is not data:
            self._raw_path_data = data
            self._raw_paths = {}
        key = (id(xs), id(values))
        path = self._raw_paths.get(key)
        if path is None:
            # Build once in data coordinates, preserving every buffered point.
            path = QPainterPath()
            started = False
            for index in range(length):
                try:
                    x = float(xs[index])
                    value = float(values[index])
                except (TypeError, ValueError):
                    started = False
                    continue
                if not math.isfinite(x) or not math.isfinite(value):
                    started = False
                    continue
                if not started:
                    path.moveTo(x, value)
                    started = True
                else:
                    path.lineTo(x, value)
            self._raw_paths[key] = path
        view_start, view_end = self.owner.view_range()
        x_scale = geometry["plot_width"] / max(0.000001, view_end - view_start)
        painter.save()
        painter.translate(geometry["left"] - view_start * x_scale, middle)
        painter.scale(x_scale, -scale)
        pen = QPen(color, 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        painter.restore()



class ReplayWindow(QMainWindow):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = Path(config_path).resolve()
        initial_config = _read_json_config(self.config_path)
        self.dataset_name = str(initial_config.get("dataset", "20251221"))
        self.dataset_root = _guess_dataset_root(self.config_path)
        initial_dataset_dir = self.dataset_root / "data" / self.dataset_name
        if initial_dataset_dir.is_dir() and (
            _contains_mfl_data(initial_dataset_dir)
            or _find_nested_dataset_layout(initial_dataset_dir) is not None
        ):
            (
                self.config_path,
                self.dataset_name,
                self.dataset_root,
            ) = _prepare_dataset_selection(initial_dataset_dir, self.config_path)
            initial_config = _read_json_config(self.config_path)
        self._pending_dataset_load: Optional[
            tuple[Path, str, Path, AnnotationStore, list[dict[str, Any]]]
        ] = None
        self._stopping_worker: Optional[DataWorker] = None
        self.annotation_store = AnnotationStore(self.config_path)
        self.annotation_groups: list[dict[str, Any]] = (
            self.annotation_store.load_event_groups()
        )
        self.model_predictions: list[dict[str, Any]] = []
        self.model_result_path: Optional[Path] = None
        self.model_result_error = ""
        self.model_result_selected_id: Optional[str] = None
        self.model_result_enabled = True
        self.model_result_filter = "anomaly"
        self.annotation_jump_index = -1
        self.annotation_mode = False
        self.annotation_layer_visible = True
        self.measure_mode = False
        self.measure_points: list[float] = []
        self.annotation_drag_start_x: Optional[float] = None
        self.annotation_drag_current_x: Optional[float] = None
        self.annotation_selection_start_m: Optional[float] = None
        self.annotation_selection_end_m: Optional[float] = None
        self.annotation_edit_id: Optional[str] = None
        self.annotation_reselecting = False
        self.default_us_alignment_offset_m = self.read_us_alignment_offset()
        self.us_alignment_offset_m = self.default_us_alignment_offset_m
        self.us_alignment_dirty = False
        self.manifest: Optional[dict[str, Any]] = None
        self.data: Optional[dict[str, Any]] = None
        self.joint_detection_status = ""
        self.worker = DataWorker(self.config_path)
        self.request_serial = 0
        self.fetch_pending = False
        self.last_request_time = 0.0
        self.desired_x_start = 0.0
        self.view_x_start = 0.0
        self.window_width = 6.0
        self.speed = 5.0
        self.cursor_x: Optional[float] = None
        self.cursor_sample: Optional[dict[str, Any]] = None
        self.cursor_request_serial = 0
        self.playing = False
        self.last_play_tick = time.monotonic()
        self.mfl_y_limits = [MFL_Y_LIMIT_DEFAULT for _ in range(16)]
        self.mfl_actual_ranges = [(0.0, 0.0) for _ in range(16)]
        self.mfl_peak_values = [0.0 for _ in range(16)]
        self.mfl_clipped_counts = [0 for _ in range(16)]
        # Use one stable scale for every channel and every playback window so
        # waveform heights remain directly comparable over time.
        self.mfl_scale_mode: Any = MFL_Y_LIMIT_DEFAULT
        self.mfl_background_mode = "light"
        # Keep Gate markers readable without letting a dense event cluster
        # dominate the ultrasound plot.
        self.ultrasound_marker_size = 8.0
        self.window_cache: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        self.request_key_by_serial: dict[int, tuple[Any, ...]] = {}
        self.pending_request_keys: set[tuple[Any, ...]] = set()
        self.prefetch_serials: set[int] = set()
        self.navigation_direction = 1
        self.buffer_target_count = 5
        self.track_boxes: dict[int, QCheckBox] = {}
        self.gate_buttons: dict[int, QToolButton] = {}
        self.update_settings_path = APP_DIR / "update_settings.json"
        self.update_repository = str(
            _read_json_config(self.update_settings_path).get(
                "github_repo",
                DEFAULT_GITHUB_REPOSITORY,
            )
            or DEFAULT_GITHUB_REPOSITORY
        )
        self.update_check_worker: Optional[ReleaseCheckWorker] = None
        self.update_download_worker: Optional[ReleaseDownloadWorker] = None
        self.latest_release: Optional[dict[str, Any]] = None
        self.request_timer = QTimer(self)
        self.request_timer.setSingleShot(True)
        self.request_timer.timeout.connect(self.request_window)
        self.prefetch_timer = QTimer(self)
        self.prefetch_timer.setSingleShot(True)
        self.prefetch_timer.timeout.connect(self.prefetch_next_buffer)
        self.cursor_timer = QTimer(self)
        self.cursor_timer.setSingleShot(True)
        self.cursor_timer.setInterval(16)
        self.cursor_timer.timeout.connect(self.request_cursor_sample)
        self.play_timer = QTimer(self)
        self.play_timer.setInterval(40)
        self.play_timer.timeout.connect(self.play_tick)
        self._navigation_delta = 0.0
        self.navigation_timer = QTimer(self)
        self.navigation_timer.setSingleShot(True)
        self.navigation_timer.setInterval(16)
        self.navigation_timer.timeout.connect(self._flush_navigation)
        self._navigation_shortcuts: list[QShortcut] = []
        self._annotation_shortcuts: list[QShortcut] = []

        self.setWindowTitle(f"超声 · 漏磁同步回放  |  {self.dataset_name}")
        self.resize(1280, 900)
        self.setMinimumSize(640, 420)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setup_ui()
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.resize(min(1280, int(area.width() * .95)), min(900, int(area.height() * .92)))
        self.connect_signals()
        self._install_navigation_shortcuts()

        self._connect_worker(self.worker)
        self.worker.start()

    def _install_navigation_shortcuts(self) -> None:
        """Make mileage navigation work even when a child control has focus."""
        bindings = (
            ("Left", -1.0),
            ("Right", 1.0),
            ("Shift+Left", -0.2),
            ("Shift+Right", 0.2),
            ("Home", None),
            ("End", None),
        )
        for sequence, delta in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ApplicationShortcut)
            if delta is None:
                shortcut.activated.connect(
                    lambda sequence=sequence: self._handle_navigation_shortcut(
                        self.data_start if sequence == "Home" else self.max_start(),
                        absolute=True,
                    )
                )
            else:
                shortcut.activated.connect(
                    lambda delta=delta: self._handle_navigation_shortcut(delta)
                )
            self._navigation_shortcuts.append(shortcut)

        # Annotation shortcuts are application-local so they also work after
        # the mouse is released on the plot or while the annotation controls
        # have focus.  The order follows ANNOTATION_LABELS, which is also the
        # order used by the annotation type combo box.
        for number, (code, name) in enumerate(ANNOTATION_LABELS.items(), 1):
            shortcut = QShortcut(QKeySequence(str(number)), self)
            shortcut.setContext(Qt.ApplicationShortcut)
            shortcut.setWhatsThis(f"选择异常类型：{number} - {name}")
            shortcut.activated.connect(
                lambda code=code, number=number: self.select_annotation_label_shortcut(
                    code,
                    number,
                )
            )
            self._annotation_shortcuts.append(shortcut)

        for key in (Qt.Key_Return, Qt.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ApplicationShortcut)
            shortcut.setWhatsThis("保存当前异常标注")
            shortcut.activated.connect(self.save_annotation_shortcut)
            self._annotation_shortcuts.append(shortcut)

    def _handle_navigation_shortcut(
        self,
        value: float,
        *,
        absolute: bool = False,
    ) -> None:
        if (
            self.manifest is None
            or not self.worker.isRunning()
            or self._stopping_worker is not None
        ):
            return
        if absolute:
            self.navigation_timer.stop()
            self._navigation_delta = 0.0
            self.set_position(value)
        else:
            self._navigation_delta += value
            if not self.navigation_timer.isActive():
                self.navigation_timer.start()

    def _flush_navigation(self) -> None:
        delta = self._navigation_delta
        self._navigation_delta = 0.0
        if self.manifest is not None and self._stopping_worker is None:
            self.move_position(delta)

    def _connect_worker(self, worker: DataWorker) -> None:
        """Connect one worker while ignoring queued signals from old data."""
        worker.ready.connect(
            lambda manifest, source=worker: (
                self.on_ready(manifest)
                if self.worker is source and self._stopping_worker is None
                else None
            )
        )
        worker.window_ready.connect(
            lambda serial, payload, source=worker: (
                self.on_window_ready(serial, payload)
                if self.worker is source and self._stopping_worker is None
                else None
            )
        )
        worker.cursor_ready.connect(
            lambda serial, payload, source=worker: (
                self.on_cursor_ready(serial, payload)
                if self.worker is source and self._stopping_worker is None
                else None
            )
        )
        worker.joint_ready.connect(
            lambda result, source=worker: (
                self.on_joint_ready(result)
                if self.worker is source and self._stopping_worker is None
                else None
            )
        )
        worker.failed.connect(
            lambda message, source=worker: (
                self.on_worker_failed(message)
                if self.worker is source and self._stopping_worker is None
                else None
            )
        )
        worker.progress.connect(
            lambda message, source=worker: (
                self.on_worker_progress(message)
                if self.worker is source and self._stopping_worker is None
                else None
            )
        )
        worker.joint_failed.connect(
            lambda message, source=worker: (
                self.on_joint_failed(message)
                if self.worker is source and self._stopping_worker is None
                else None
            )
        )
        worker.joint_progress.connect(
            lambda message, source=worker: (
                self.on_joint_progress(message)
                if self.worker is source and self._stopping_worker is None
                else None
            )
        )
        worker.ultrasound_joint_ready.connect(
            lambda result, source=worker: (
                self.on_ultrasound_joint_ready(result)
                if self.worker is source and self._stopping_worker is None
                else None
            )
        )
        worker.ultrasound_joint_failed.connect(
            lambda message, source=worker: (
                self.on_ultrasound_joint_failed(message)
                if self.worker is source and self._stopping_worker is None
                else None
            )
        )
        worker.ultrasound_joint_progress.connect(
            lambda message, source=worker: (
                self.on_ultrasound_joint_progress(message)
                if self.worker is source and self._stopping_worker is None
                else None
            )
        )

    def read_us_alignment_offset(self) -> float:
        """Read the optional persisted live ultrasound display offset."""
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            alignment = config.get("alignment", {})
            value = float(alignment.get("ultrasound_offset_m", 0.0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            value = 0.0
        return clamp(
            value,
            -US_ALIGNMENT_OFFSET_LIMIT_M,
            US_ALIGNMENT_OFFSET_LIMIT_M,
        )

    def read_model_result_config(self) -> tuple[Optional[Path], str]:
        """Return the configured model CSV and its default MFL segment."""
        try:
            config = json.loads(
                self.config_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None, "EX01"
        raw_path = config.get("model_result_path")
        segment = str(config.get("model_result_segment", "EX01")).strip() or "EX01"
        if not raw_path:
            return None, segment
        result_path = Path(str(raw_path))
        if not result_path.is_absolute():
            result_path = self.config_path.parent / result_path
        return result_path.resolve(), segment

    def setup_ui_legacy(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 8, 10, 8)
        root_layout.setSpacing(8)

        header = QFrame()
        header.setObjectName("HeaderFrame")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 9, 14, 9)
        header_layout.setSpacing(14)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        eyebrow = QLabel("SYNCHRONIZED DATA REPLAY")
        eyebrow.setObjectName("Eyebrow")
        title = QLabel("20251221 超声—漏磁同步回放")
        title.setObjectName("Title")
        subtitle = QLabel("接头为零点 · 共享相对距离横轴 · 直接读取本地数据")
        subtitle.setObjectName("Subtitle")
        title_box.addWidget(eyebrow)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box, 1)

        self.health_label = QLabel("正在加载数据…")
        self.health_label.setObjectName("Health")
        self.health_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.load_progress = QProgressBar()
        self.load_progress.setObjectName("LoadProgress")
        self.load_progress.setRange(0, 1000)
        self.load_progress.setValue(0)
        self.load_progress.setFormat("加载中 0.0%")
        self.load_progress.setTextVisible(True)
        self.load_progress.setFixedWidth(178)
        self.load_progress.setToolTip("当前数据加载进度")
        header_layout.addWidget(self.load_progress)
        header_layout.addWidget(self.health_label)
        root_layout.addWidget(header)

        control_frame = QFrame()
        control_frame.setObjectName("ControlFrame")
        control_layout = QVBoxLayout(control_frame)
        control_layout.setContentsMargins(10, 7, 10, 7)
        control_layout.setSpacing(6)

        first_row = QHBoxLayout()
        first_row.setSpacing(6)
        self.play_button = QPushButton("▶ 开始回放")
        self.play_button.setObjectName("PrimaryButton")
        self.play_button.setEnabled(False)
        self.joint_button = QPushButton("一键定位接头")
        self.joint_button.setToolTip(
            "扫描所有漏磁数据，定位 abs(DAT.value) 全局最大值。"
        )
        self.ultrasound_joint_button = QPushButton("检测超声接头")
        self.ultrasound_joint_button.setToolTip(
            "扫描超声多 Gate、双 Track 回波簇；确认后写入当前数据配置并重新对齐。"
        )
        self.home_button = QPushButton("|◀")
        self.home_button.setToolTip("跳到数据起点")
        self.end_button = QPushButton("▶|")
        self.end_button.setToolTip("跳到数据终点")
        for button in (
            self.joint_button,
            self.ultrasound_joint_button,
            self.home_button,
            self.end_button,
        ):
            button.setEnabled(False)
        first_row.addWidget(self.play_button)
        first_row.addWidget(self.joint_button)
        first_row.addWidget(self.ultrasound_joint_button)
        first_row.addWidget(self.home_button)
        first_row.addWidget(self.end_button)

        first_row.addSpacing(10)
        first_row.addWidget(QLabel("窗口"))
        self.window_combo = QComboBox()
        self.window_combo.addItems(["3 m", "5 m", "6 m", "8 m", "10 m", "20 m", "50 m", "100 m"])
        self.window_combo.setCurrentText("6 m")
        self.window_combo.setEnabled(False)
        first_row.addWidget(self.window_combo)

        first_row.addWidget(QLabel("速度"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["1 m/s", "2 m/s", "5 m/s", "10 m/s", "20 m/s"])
        self.speed_combo.setCurrentText("5 m/s")
        self.speed_combo.setEnabled(False)
        first_row.addWidget(self.speed_combo)

        first_row.addWidget(QLabel("起点"))
        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(-1_000_000_000.0, 1_000_000_000.0)
        self.start_spin.setDecimals(2)
        self.start_spin.setSingleStep(0.1)
        self.start_spin.setSuffix(" m")
        self.start_spin.setEnabled(False)
        self.start_spin.setMinimumWidth(130)
        first_row.addWidget(self.start_spin)
        self.jump_button = QPushButton("跳转")
        self.jump_button.setEnabled(False)
        first_row.addWidget(self.jump_button)
        first_row.addStretch(1)
        control_layout.addLayout(first_row)

        second_row = QHBoxLayout()
        second_row.setSpacing(8)
        track_label = QLabel("Track")
        track_label.setObjectName("FilterLabel")
        second_row.addWidget(track_label)
        for track in (0, 1):
            checkbox = QCheckBox(f"Track {track}")
            checkbox.setChecked(True)
            checkbox.setEnabled(False)
            self.track_boxes[track] = checkbox
            second_row.addWidget(checkbox)

        second_row.addSpacing(8)
        second_row.addWidget(QLabel("Gate", objectName="FilterLabel"))
        gate_scroll = QScrollArea()
        gate_scroll.setObjectName("GateScroll")
        gate_scroll.setWidgetResizable(True)
        gate_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        gate_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        gate_scroll.setFrameShape(QFrame.NoFrame)
        gate_scroll.setFixedHeight(34)
        gate_widget = QWidget()
        self.gate_layout = QHBoxLayout(gate_widget)
        self.gate_layout.setContentsMargins(0, 0, 0, 0)
        self.gate_layout.setSpacing(4)
        self.gate_layout.addWidget(QLabel("读取中…"))
        self.gate_layout.addStretch(1)
        gate_scroll.setWidget(gate_widget)
        second_row.addWidget(gate_scroll, 1)

        self.select_gates_button = QPushButton("全选")
        self.clear_gates_button = QPushButton("清空")
        self.select_gates_button.setEnabled(False)
        self.clear_gates_button.setEnabled(False)
        second_row.addWidget(self.select_gates_button)
        second_row.addWidget(self.clear_gates_button)
        control_layout.addLayout(second_row)

        self.timeline = QSlider(Qt.Horizontal)
        self.timeline.setRange(0, 100000)
        self.timeline.setValue(0)
        self.timeline.setEnabled(False)
        control_layout.addWidget(self.timeline)
        self.range_label = QLabel("范围：等待数据…")
        self.range_label.setObjectName("RangeLabel")
        control_layout.addWidget(self.range_label)
        root_layout.addWidget(control_frame)

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(5)
        self.us_plot = UltrasoundPlot(self)
        self.mfl_plot = MflPlot(self)
        self.us_plot.setMinimumHeight(180)
        self.mfl_plot.setMinimumHeight(round(360 * MFL_PLOT_HEIGHT_FACTOR))
        self.splitter.addWidget(self.us_plot)
        self.splitter.addWidget(self.mfl_plot)
        self.splitter.setSizes([250, round(520 * MFL_PLOT_HEIGHT_FACTOR)])
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 14)
        root_layout.addWidget(self.splitter, 1)

        footer = QHBoxLayout()
        self.cursor_label = QLabel("光标：—")
        self.data_label = QLabel("数据：等待加载…")
        self.server_label = QLabel("服务：后台读取线程")
        footer.addWidget(self.cursor_label)
        footer.addWidget(self.data_label)
        footer.addStretch(1)
        footer.addWidget(self.server_label)
        root_layout.addLayout(footer)

    def setup_ui_legacy_layout(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(10, 6, 10, 8)
        root_layout.setSpacing(8)
        # Loading details already appear in the sidebar; keep only a hidden
        # compatibility label for the existing status-update code.
        self.health_label = QLabel(root)
        self.health_label.hide()
        body = QHBoxLayout()
        body.setSpacing(8)
        side_panel = QFrame()
        side_panel.setObjectName("SidePanel")
        # Keep the side panel wide enough for status lines and filter labels.
        # The plots retain a separate minimum width, so this does not change
        # the shared x-axis geometry between the two modalities.
        side_panel.setMinimumWidth(320)
        side_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        side_layout = QVBoxLayout(side_panel)
        side_layout.setContentsMargins(8, 8, 8, 8)
        side_layout.setSpacing(8)
        control_group = QGroupBox("\u56de\u653e\u63a7\u5236")
        control_layout = QGridLayout(control_group)
        control_layout.setContentsMargins(8, 14, 8, 8)
        control_layout.setHorizontalSpacing(6)
        control_layout.setVerticalSpacing(6)
        self.play_button = QPushButton("\u25b6  \u5f00\u59cb\u56de\u653e")
        self.play_button.setObjectName("PrimaryButton")
        self.play_button.setEnabled(False)
        self.joint_button = QPushButton("\u4e00\u952e\u5b9a\u4f4d\u63a5\u5934")
        self.joint_button.setToolTip(
            "\u626b\u63cf\u6240\u6709\u6f0f\u78c1\u6570\u636e\uff0c\u5b9a\u4f4d abs(DAT.value) \u5168\u5c40\u6700\u5927\u503c\u3002"
        )
        self.ultrasound_joint_button = QPushButton("\u68c0\u6d4b\u8d85\u58f0\u63a5\u5934")
        self.ultrasound_joint_button.setToolTip(
            "\u626b\u63cf\u8d85\u58f0\u591a Gate\u3001\u53cc Track \u56de\u6ce2\u7c07\uff1b"
            "\u786e\u8ba4\u540e\u5199\u5165\u5f53\u524d\u6570\u636e\u914d\u7f6e\u5e76\u91cd\u65b0\u5bf9\u9f50\u3002"
        )
        self.home_button = QPushButton("\u23ee")
        self.home_button.setToolTip("\u8df3\u5230\u6570\u636e\u8d77\u70b9")
        self.end_button = QPushButton("\u23ed")
        self.end_button.setToolTip("\u8df3\u5230\u6570\u636e\u7ec8\u70b9")
        for button in (
            self.joint_button,
            self.ultrasound_joint_button,
            self.home_button,
            self.end_button,
        ):
            button.setEnabled(False)
        control_layout.addWidget(self.play_button, 0, 0, 1, 4)
        control_layout.addWidget(self.joint_button, 1, 0)
        control_layout.addWidget(self.ultrasound_joint_button, 1, 1)
        control_layout.addWidget(self.home_button, 1, 2)
        control_layout.addWidget(self.end_button, 1, 3)
        control_layout.addWidget(QLabel("\u7a97\u53e3"), 2, 0)
        self.window_combo = QComboBox()
        self.window_combo.addItems(["3 m", "5 m", "6 m", "8 m", "10 m", "20 m", "50 m", "100 m"])
        self.window_combo.setCurrentText("6 m")
        self.window_combo.setEnabled(False)
        control_layout.addWidget(self.window_combo, 2, 1, 1, 3)
        control_layout.addWidget(QLabel("\u901f\u5ea6"), 3, 0)
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["1 m/s", "2 m/s", "5 m/s", "10 m/s", "20 m/s"])
        self.speed_combo.setCurrentText("5 m/s")
        self.speed_combo.setEnabled(False)
        control_layout.addWidget(self.speed_combo, 3, 1, 1, 3)
        control_layout.addWidget(QLabel("\u8d77\u70b9"), 4, 0)
        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(-1_000_000_000.0, 1_000_000_000.0)
        self.start_spin.setDecimals(2)
        self.start_spin.setSingleStep(0.1)
        self.start_spin.setSuffix(" m")
        self.start_spin.setEnabled(False)
        self.start_spin.setMinimumWidth(118)
        control_layout.addWidget(self.start_spin, 4, 1, 1, 3)
        self.jump_button = QPushButton("\u8df3\u8f6c")
        self.jump_button.setEnabled(False)
        control_layout.addWidget(self.jump_button, 5, 0, 1, 4)
        self.timeline = QSlider(Qt.Horizontal)
        self.timeline.setRange(0, 100000)
        self.timeline.setValue(0)
        self.timeline.setEnabled(False)
        control_layout.addWidget(self.timeline, 6, 0, 1, 4)
        self.range_label = QLabel("\u8303\u56f4\uff1a\u7b49\u5f85\u6570\u636e")
        self.range_label.setObjectName("RangeLabel")
        self.range_label.setWordWrap(True)
        control_layout.addWidget(self.range_label, 7, 0, 1, 4)
        distance_jump_layout = QHBoxLayout()
        self.back_100_button = QPushButton("← 后退 100 m")
        self.forward_100_button = QPushButton("前进 100 m →")
        for button, delta in ((self.back_100_button, -100.0), (self.forward_100_button, 100.0)):
            button.setEnabled(False)
            button.setToolTip(f"当前窗口沿共享里程移动 {delta:+.0f} 米；到数据边界停止")
            button.clicked.connect(lambda checked=False, step=delta: self.move_position(step))
            distance_jump_layout.addWidget(button)
        control_layout.addLayout(distance_jump_layout, 8, 0, 1, 4)
        side_layout.addWidget(control_group)
        filter_group = QGroupBox("\u8d85\u58f0\u7b5b\u9009")
        filter_layout = QVBoxLayout(filter_group)
        filter_layout.setContentsMargins(8, 14, 8, 8)
        filter_layout.setSpacing(6)
        track_label = QLabel("Track  /  \u8f68\u9053")
        track_label.setObjectName("FilterLabel")
        filter_layout.addWidget(track_label)
        track_layout = QHBoxLayout()
        track_layout.setSpacing(8)
        for track in (0, 1):
            track_text = "Track 0  LOWER" if track == 0 else "Track 1  UPPER"
            checkbox = QCheckBox(track_text)
            checkbox.setChecked(True)
            checkbox.setEnabled(False)
            self.track_boxes[track] = checkbox
            checkbox.stateChanged.connect(self.schedule_request)
            track_layout.addWidget(checkbox)
        filter_layout.addLayout(track_layout)
        gate_label = QLabel("Gate  /  \u68c0\u6d4b\u95e8")
        gate_label.setObjectName("FilterLabel")
        filter_layout.addWidget(gate_label)
        gate_scroll = QScrollArea()
        gate_scroll.setObjectName("GateScroll")
        gate_scroll.setWidgetResizable(True)
        gate_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        gate_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        gate_scroll.setFrameShape(QFrame.NoFrame)
        gate_scroll.setMinimumHeight(120)
        gate_scroll.setMaximumHeight(150)
        gate_widget = QWidget()
        self.gate_layout = QGridLayout(gate_widget)
        self.gate_layout.setContentsMargins(2, 2, 2, 2)
        self.gate_layout.setHorizontalSpacing(4)
        self.gate_layout.setVerticalSpacing(4)
        self.gate_layout.addWidget(QLabel("\u8bfb\u53d6\u4e2d\u2026"), 0, 0)
        gate_scroll.setWidget(gate_widget)
        filter_layout.addWidget(gate_scroll, 1)
        gate_button_layout = QHBoxLayout()
        self.select_gates_button = QPushButton("\u5168\u9009")
        self.clear_gates_button = QPushButton("\u6e05\u7a7a")
        self.select_gates_button.setEnabled(False)
        self.clear_gates_button.setEnabled(False)
        gate_button_layout.addWidget(self.select_gates_button)
        gate_button_layout.addWidget(self.clear_gates_button)
        filter_layout.addLayout(gate_button_layout)
        # Do not let the gate list consume all spare height; the information
        # group below needs stable room for readable, wrapped status lines.
        side_layout.addWidget(filter_group)
        display_group = QGroupBox("\u663e\u793a\u8bbe\u7f6e")
        display_layout = QGridLayout(display_group)
        display_layout.setContentsMargins(8, 14, 8, 8)
        display_layout.setHorizontalSpacing(6)
        display_layout.setVerticalSpacing(6)
        display_layout.addWidget(QLabel("MFL \u91cf\u7a0b"), 0, 0)
        self.mfl_scale_combo = QComboBox()
        self.mfl_scale_combo.addItem("\u81ea\u52a8\uff08\u6bcf\u884c\uff09", "auto")
        for value in (0.5, 1.0, 2.0, 4.0, 8.0, 16.0):
            self.mfl_scale_combo.addItem(f"\u7edf\u4e00 \u00b1{value:g}", value)
        self.mfl_scale_combo.setCurrentIndex(self.mfl_scale_combo.findData(MFL_Y_LIMIT_DEFAULT))
        self.mfl_scale_combo.setEnabled(False)
        display_layout.addWidget(self.mfl_scale_combo, 0, 1)
        display_layout.addWidget(QLabel("\u6f0f\u78c1\u5e95\u8272"), 1, 0)
        self.mfl_background_combo = QComboBox()
        self.mfl_background_combo.addItem("\u6df1\u8272", "dark")
        self.mfl_background_combo.addItem("\u767d\u8272", "light")
        self.mfl_background_combo.setCurrentIndex(self.mfl_background_combo.findData("light"))
        self.mfl_background_combo.setEnabled(False)
        display_layout.addWidget(self.mfl_background_combo, 1, 1)
        display_layout.addWidget(QLabel("\u8d85\u58f0\u6807\u8bb0\u5927\u5c0f"), 2, 0)
        self.marker_size_combo = QSpinBox()
        self.marker_size_combo.setRange(6, 20)
        self.marker_size_combo.setSingleStep(1)
        self.marker_size_combo.setValue(8)
        self.marker_size_combo.setSuffix(" px")
        self.marker_size_combo.setEnabled(False)
        display_layout.addWidget(self.marker_size_combo, 2, 1)
        self.gate_legend_button = QPushButton("\u67e5\u770b\u8d85\u58f0\u6807\u8bb0\u5bf9\u7167\u8868")
        self.gate_legend_button.setEnabled(False)
        display_layout.addWidget(self.gate_legend_button, 3, 0, 1, 2)
        side_layout.addWidget(display_group)
        alignment_group = QGroupBox("\u540c\u6b65\u6821\u51c6")
        alignment_layout = QGridLayout(alignment_group)
        alignment_layout.setContentsMargins(8, 14, 8, 8)
        alignment_layout.setHorizontalSpacing(6)
        alignment_layout.setVerticalSpacing(6)
        alignment_layout.addWidget(QLabel("\u8d85\u58f0\u504f\u79fb"), 0, 0)
        self.us_alignment_spin = QDoubleSpinBox()
        self.us_alignment_spin.setRange(
            -US_ALIGNMENT_OFFSET_LIMIT_M,
            US_ALIGNMENT_OFFSET_LIMIT_M,
        )
        self.us_alignment_spin.setDecimals(3)
        self.us_alignment_spin.setSingleStep(US_ALIGNMENT_FINE_STEP_M)
        self.us_alignment_spin.setSuffix(" m")
        self.us_alignment_spin.setValue(self.us_alignment_offset_m)
        self.us_alignment_spin.setEnabled(False)
        self.us_alignment_spin.setToolTip(
            "\u4ee5\u6f0f\u78c1\u4e3a\u57fa\u51c6\uff0c\u6b63\u503c\u4f7f\u8d85\u58f0\u6574\u4f53\u5411\u53f3\u79fb"
        )
        alignment_layout.addWidget(self.us_alignment_spin, 0, 1, 1, 2)

        self.us_alignment_fine_left_button = QPushButton("\u5de6\u79fb 1 mm")
        self.us_alignment_fine_right_button = QPushButton("\u53f3\u79fb 1 mm")
        self.us_alignment_coarse_left_button = QPushButton("\u5de6\u79fb 10 mm")
        self.us_alignment_coarse_right_button = QPushButton("\u53f3\u79fb 10 mm")
        for button in (
            self.us_alignment_fine_left_button,
            self.us_alignment_fine_right_button,
            self.us_alignment_coarse_left_button,
            self.us_alignment_coarse_right_button,
        ):
            button.setEnabled(False)
        alignment_layout.addWidget(self.us_alignment_fine_left_button, 1, 0)
        alignment_layout.addWidget(self.us_alignment_fine_right_button, 1, 1)
        alignment_layout.addWidget(self.us_alignment_coarse_left_button, 2, 0)
        alignment_layout.addWidget(self.us_alignment_coarse_right_button, 2, 1)

        self.us_alignment_reset_button = QPushButton("\u6062\u590d\u542f\u52a8\u503c")
        self.us_alignment_save_button = QPushButton("\u4fdd\u5b58\u5f53\u524d\u6821\u51c6")
        self.us_alignment_reset_button.setEnabled(False)
        self.us_alignment_save_button.setEnabled(False)
        alignment_layout.addWidget(self.us_alignment_reset_button, 3, 0)
        alignment_layout.addWidget(self.us_alignment_save_button, 3, 1, 1, 2)
        self.us_alignment_hint = QLabel(
            "\u4ee5\u6f0f\u78c1\u4e3a\u57fa\u51c6\uff1b\u6b63\u503c=\u8d85\u58f0\u5411\u53f3\u79fb\u3002\u8c03\u6574\u53ea\u6539\u53d8\u663e\u793a\u5750\u6807\uff0c\u4e0d\u4fee\u6539\u539f\u59cb\u6570\u636e\u3002"
        )
        self.us_alignment_hint.setWordWrap(True)
        self.us_alignment_hint.setObjectName("InfoLabel")
        alignment_layout.addWidget(self.us_alignment_hint, 4, 0, 1, 3)
        side_layout.addWidget(alignment_group)

        update_group = QGroupBox("软件更新")
        update_layout = QGridLayout(update_group)
        update_layout.setContentsMargins(8, 14, 8, 8)
        update_layout.setHorizontalSpacing(6)
        update_layout.setVerticalSpacing(6)
        self.update_version_label = QLabel(f"当前版本：{APP_VERSION}")
        update_layout.addWidget(self.update_version_label, 0, 0, 1, 2)
        update_layout.addWidget(QLabel("GitHub 仓库"), 1, 0)
        self.update_repository_edit = QLineEdit(self.update_repository)
        self.update_repository_edit.setPlaceholderText("owner/repository")
        self.update_repository_edit.setToolTip(
            "填写公开 GitHub 仓库，例如 your-name/your-replay-app"
        )
        update_layout.addWidget(self.update_repository_edit, 1, 1)
        self.update_save_repository_button = QPushButton("保存仓库")
        self.update_check_button = QPushButton("检查更新")
        self.update_check_button.setObjectName("PrimaryButton")
        update_action_layout = QHBoxLayout()
        update_action_layout.addWidget(self.update_save_repository_button)
        update_action_layout.addWidget(self.update_check_button)
        update_layout.addLayout(update_action_layout, 2, 0, 1, 2)
        self.update_status_label = QLabel(
            "先填写 GitHub 仓库 owner/repository。"
            if not self.update_repository
            else "可以检查 GitHub Release 中是否有新版本。"
        )
        self.update_status_label.setWordWrap(True)
        self.update_status_label.setObjectName("InfoLabel")
        update_layout.addWidget(self.update_status_label, 3, 0, 1, 2)
        self.update_progress_bar = QProgressBar()
        self.update_progress_bar.setRange(0, 1000)
        self.update_progress_bar.setValue(0)
        self.update_progress_bar.setVisible(False)
        update_layout.addWidget(self.update_progress_bar, 4, 0, 1, 2)
        side_layout.addWidget(update_group)

        measure_group = QGroupBox("信号距离测量")
        measure_layout = QVBoxLayout(measure_group)
        self.measure_button = QPushButton("测量两点距离")
        self.measure_button.setCheckable(True)
        self.measure_button.toggled.connect(self.toggle_measurement)
        measure_layout.addWidget(self.measure_button)
        measure_clear = QPushButton("清除测量")
        measure_clear.clicked.connect(self.clear_measurement)
        measure_layout.addWidget(measure_clear)
        self.measure_label = QLabel("开启后在超声或漏磁图中依次点击两个信号。\n测量沿线路的距离，精度取决于里程标定。")
        self.measure_label.setWordWrap(True)
        measure_layout.addWidget(self.measure_label)
        side_layout.addWidget(measure_group)

        model_group = QGroupBox("模型异常候选 / 频率提示")
        model_layout = QGridLayout(model_group)
        model_layout.setContentsMargins(8, 14, 8, 8)
        model_layout.setHorizontalSpacing(6)
        model_layout.setVerticalSpacing(6)
        self.model_result_check = QCheckBox("显示模型结果")
        self.model_result_check.setChecked(True)
        self.model_result_check.setEnabled(False)
        model_layout.addWidget(self.model_result_check, 0, 0, 1, 3)
        model_layout.addWidget(QLabel("筛选"), 1, 0)
        self.model_result_filter_combo = QComboBox()
        self.model_result_filter_combo.addItem("全部异常候选", "anomaly")
        self.model_result_filter_combo.addItem("左侧候选", "left")
        self.model_result_filter_combo.addItem("右侧候选", "right")
        self.model_result_filter_combo.addItem(
            "频率提示：振动干扰",
            "frequency_hint:vibration_interference",
        )
        self.model_result_filter_combo.addItem(
            "频率提示：焊缝",
            "frequency_hint:weld",
        )
        self.model_result_filter_combo.addItem(
            "形态提示：疑似斜裂纹",
            "frequency_hint:suspected_oblique_crack",
        )
        self.model_result_filter_combo.setEnabled(False)
        model_layout.addWidget(self.model_result_filter_combo, 1, 1, 1, 2)
        self.model_result_previous_button = QPushButton("上一处")
        self.model_result_next_button = QPushButton("下一处")
        self.model_result_previous_button.setEnabled(False)
        self.model_result_next_button.setEnabled(False)
        model_layout.addWidget(self.model_result_previous_button, 2, 0)
        model_layout.addWidget(self.model_result_next_button, 2, 1)
        self.model_result_reload_button = QPushButton("重新加载")
        self.model_result_reload_button.setEnabled(False)
        model_layout.addWidget(self.model_result_reload_button, 2, 2)
        self.model_result_status_label = QLabel("未加载模型结果")
        self.model_result_status_label.setObjectName("InfoLabel")
        self.model_result_status_label.setWordWrap(True)
        model_layout.addWidget(self.model_result_status_label, 3, 0, 1, 3)
        self.model_result_detail_label = QLabel(
            "模型异常候选会叠加在超声和漏磁图上；若结果含 frequency_hint，"
            "仅作为频率提示显示，人工标注文件不会被覆盖。"
        )
        self.model_result_detail_label.setObjectName("InfoLabel")
        self.model_result_detail_label.setWordWrap(True)
        model_layout.addWidget(self.model_result_detail_label, 4, 0, 1, 3)
        self.model_result_open_button = QPushButton("选择结果文件…")
        self.model_result_open_button.setEnabled(False)
        model_layout.addWidget(self.model_result_open_button, 5, 0, 1, 3)
        side_layout.addWidget(model_group)

        annotation_group = QGroupBox("人工标注（只标异常）")
        annotation_layout = QGridLayout(annotation_group)
        annotation_layout.setContentsMargins(8, 14, 8, 8)
        annotation_layout.setHorizontalSpacing(6)
        annotation_layout.setVerticalSpacing(6)
        self.annotation_mode_button = QPushButton("开始框选漏磁区间")
        self.annotation_mode_button.setCheckable(True)
        self.annotation_mode_button.setEnabled(False)
        self.annotation_mode_button.setToolTip(
            "点击后，在下方漏磁图上按住鼠标左键拖出异常区间"
        )
        annotation_layout.addWidget(self.annotation_mode_button, 0, 0, 1, 3)

        annotation_layout.addWidget(QLabel("异常类型"), 1, 0)
        self.annotation_label_combo = QComboBox()
        for number, (code, name) in enumerate(ANNOTATION_LABELS.items(), 1):
            self.annotation_label_combo.addItem(f"{number} · {name}", code)
        self.annotation_label_combo.setEnabled(False)
        self.annotation_label_combo.setToolTip(
            "可用数字键快速选择："
            + "；".join(
                f"{number}={name}"
                for number, name in enumerate(ANNOTATION_LABELS.values(), 1)
            )
            + "。按 Enter 保存。"
        )
        annotation_layout.addWidget(self.annotation_label_combo, 1, 1, 1, 2)

        annotation_layout.addWidget(QLabel("标注范围"), 2, 0)
        self.annotation_scope_combo = QComboBox()
        self.annotation_scope_combo.addItem("左右两侧 · 全部通道", "all")
        self.annotation_scope_combo.addItem("仅左侧通道", "left")
        self.annotation_scope_combo.addItem("仅右侧通道", "right")
        self.annotation_scope_combo.setEnabled(False)
        annotation_layout.addWidget(self.annotation_scope_combo, 2, 1, 1, 2)

        annotation_layout.addWidget(QLabel("方向"), 3, 0)
        self.annotation_direction_combo = QComboBox()
        self.annotation_direction_combo.addItem("X + Z", "xz")
        self.annotation_direction_combo.addItem("仅 X", "x")
        self.annotation_direction_combo.addItem("仅 Z", "z")
        self.annotation_direction_combo.setEnabled(False)
        annotation_layout.addWidget(self.annotation_direction_combo, 3, 1, 1, 2)

        self.annotation_selection_label = QLabel("选中区间：未选择")
        self.annotation_selection_label.setObjectName("InfoLabel")
        self.annotation_selection_label.setWordWrap(True)
        annotation_layout.addWidget(self.annotation_selection_label, 4, 0, 1, 3)

        self.annotation_shortcut_hint = QLabel(
            "快捷键："
            + " · ".join(
                f"{number} {name}"
                for number, name in enumerate(ANNOTATION_LABELS.values(), 1)
            )
            + " · Enter 保存"
        )
        self.annotation_shortcut_hint.setObjectName("InfoLabel")
        self.annotation_shortcut_hint.setWordWrap(True)
        annotation_layout.addWidget(self.annotation_shortcut_hint, 7, 0, 1, 3)

        self.annotation_save_button = QPushButton("保存异常标注")
        self.annotation_save_button.setObjectName("PrimaryButton")
        self.annotation_save_button.setEnabled(False)
        self.annotation_clear_button = QPushButton("清除选区")
        self.annotation_clear_button.setEnabled(False)
        annotation_layout.addWidget(self.annotation_save_button, 5, 0, 1, 2)
        annotation_layout.addWidget(self.annotation_clear_button, 5, 2)

        self.annotation_reselect_button = QPushButton("重新框选范围")
        self.annotation_reselect_button.setEnabled(False)
        self.annotation_reselect_button.setToolTip(
            "修改当前标注的范围；重新拖选后点击更新异常标注"
        )
        annotation_layout.addWidget(self.annotation_reselect_button, 6, 0, 1, 3)

        self.annotation_status_label = QLabel(
            "点击已有彩色标注可加载编辑；支持五种异常类型。"
        )
        self.annotation_status_label.setObjectName("InfoLabel")
        self.annotation_status_label.setWordWrap(True)
        annotation_layout.addWidget(self.annotation_status_label, 9, 0, 1, 3)

        self.annotation_list = QListWidget()
        self.annotation_list.setMinimumHeight(74)
        self.annotation_list.setMaximumHeight(126)
        self.annotation_list.setToolTip(
            "点击列表项或图上的彩色区域可加载标注；加载后可修改类型、方向或范围"
        )
        annotation_layout.addWidget(self.annotation_list, 10, 0, 1, 3)
        annotation_layout.addWidget(QLabel("跳转类型"), 11, 0)
        self.annotation_jump_combo = QComboBox()
        self.annotation_jump_combo.addItem("全部异常", "")
        for code, name in ANNOTATION_LABELS.items():
            self.annotation_jump_combo.addItem(name, code)
        self.annotation_jump_combo.setEnabled(False)
        self.annotation_jump_combo.setToolTip("选择异常类型后，使用上一处/下一处定位")
        annotation_layout.addWidget(self.annotation_jump_combo, 11, 1, 1, 2)
        self.annotation_jump_previous_button = QPushButton("上一处")
        self.annotation_jump_next_button = QPushButton("下一处")
        self.annotation_jump_previous_button.setEnabled(False)
        self.annotation_jump_next_button.setEnabled(False)
        annotation_layout.addWidget(self.annotation_jump_previous_button, 12, 0)
        annotation_layout.addWidget(self.annotation_jump_next_button, 12, 1)
        self.annotation_jump_status_label = QLabel("共 0 处")
        self.annotation_jump_status_label.setObjectName("InfoLabel")
        self.annotation_jump_status_label.setAlignment(Qt.AlignCenter)
        annotation_layout.addWidget(self.annotation_jump_status_label, 12, 2)
        self.annotation_delete_button = QPushButton("删除选中标注")
        self.annotation_delete_button.setEnabled(False)
        annotation_layout.addWidget(self.annotation_delete_button, 13, 0, 1, 3)
        side_layout.addWidget(annotation_group)

        info_group = QGroupBox("\u5f53\u524d\u7a97\u53e3\u4fe1\u606f")
        info_layout = QVBoxLayout(info_group)
        info_layout.setContentsMargins(8, 14, 8, 8)
        info_layout.setSpacing(8)
        self.choose_dataset_button = QPushButton("选择数据并加载…")
        self.choose_dataset_button.setToolTip(
            "选择一个portable-release目录，或直接选择 data\\数据集名；"
            "当前窗口会切换到所选数据，不修改公共 config.json。"
        )
        info_layout.addWidget(self.choose_dataset_button)
        self.load_progress = QProgressBar()
        self.load_progress.setObjectName("LoadProgress")
        self.load_progress.setRange(0, 1000)
        self.load_progress.setValue(0)
        self.load_progress.setFormat("加载中 0.0%")
        self.load_progress.setTextVisible(True)
        self.load_progress.setToolTip("当前数据加载进度")
        info_layout.addWidget(self.load_progress)
        def add_info_section(title: str) -> None:
            section = QLabel(title)
            section.setObjectName("InfoSection")
            info_layout.addWidget(section)

        def add_info_label(label: QLabel, object_name: str = "InfoLabel") -> None:
            label.setWordWrap(True)
            label.setObjectName(object_name)
            label.setMinimumHeight(22)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            info_layout.addWidget(label)

        add_info_section("数据状态")
        self.data_label = QLabel("\u6570\u636e\uff1a\u7b49\u5f85\u52a0\u8f7d")
        add_info_label(self.data_label)
        self.server_label = QLabel("\u72b6\u6001\uff1a\u6b63\u5728\u8bfb\u53d6")
        add_info_label(self.server_label)
        add_info_section("当前位置")
        self.cursor_label = QLabel("\u5149\u6807\uff1a\u2014")
        add_info_label(self.cursor_label)
        add_info_section("接头定位")
        self.joint_label = QLabel("\u672a\u5b9a\u4f4d\uff08\u70b9\u51fb\u4e0a\u65b9\u6309\u94ae\uff09")
        add_info_label(self.joint_label, "JointStatus")
        self.us_joint_anchor_label = QLabel("\u8d85\u58f0\u914d\u7f6e\u63a5\u5934 ID\uff1a--")
        add_info_label(self.us_joint_anchor_label)
        add_info_section("显示说明")
        self.mfl_scale_label = QLabel("MFL\uff1a\u7b49\u5f85\u7a97\u53e3")
        add_info_label(self.mfl_scale_label)
        self.marker_hint_label = QLabel(
            "Gate \u6807\u8bb0\uff1a\u70b9\u51fb\u56fe\u4e0a\u6807\u8bb0\u67e5\u770b\u8bf4\u660e\u3002"
        )
        add_info_label(self.marker_hint_label)
        self.server_label.setMaximumHeight(38)
        self.joint_label.setMinimumHeight(34)
        self.joint_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        side_layout.addWidget(info_group)
        side_layout.addStretch(1)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(5)
        self.us_plot = UltrasoundPlot(self)
        self.mfl_plot = MflPlot(self)
        self.us_plot.setMinimumHeight(180)
        self.mfl_plot.setMinimumHeight(round(360 * MFL_PLOT_HEIGHT_FACTOR))
        self.splitter.addWidget(self.us_plot)
        self.splitter.addWidget(self.mfl_plot)
        self.splitter.setSizes([250, round(520 * MFL_PLOT_HEIGHT_FACTOR)])
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 14)
        # The sidebar keeps its own vertical scroll range. This prevents its
        # group boxes from being compressed into overlapping controls when
        # the main window is made shorter or DPI scaling increases text size.
        side_scroll = QScrollArea()
        side_scroll.setObjectName("SidePanelScroll")
        side_scroll.setWidgetResizable(True)
        side_scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        side_scroll.setFrameShape(QFrame.NoFrame)
        side_scroll.setFixedWidth(352)
        side_scroll.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        side_scroll.setWidget(side_panel)
        body.addWidget(side_scroll)
        body.addWidget(self.splitter, 1)
        root_layout.addLayout(body, 1)

    def setup_ui(self) -> None:
        """Build the VS Code-style shell around the existing replay widgets.

        The legacy builder still owns the detailed controls and keeps their
        signal wiring unchanged.  This wrapper only rearranges those widgets
        into switchable tool pages, which keeps the data and interaction
        behavior stable while making the workspace much easier to scan.
        """
        self.setup_ui_legacy_layout()

        # Functional accents inspired by GitHub Primer's dark palette.
        for role, widgets in (
            (
                "accent",
                (
                    self.choose_dataset_button,
                    self.play_button,
                    self.jump_button,
                    self.annotation_mode_button,
                ),
            ),
            ("success", (self.annotation_save_button, self.us_alignment_save_button)),
            ("warning", (self.us_alignment_reset_button,)),
            ("danger", (self.annotation_delete_button,)),
            ("model", (self.model_result_open_button, self.model_result_next_button,
                       self.model_result_previous_button)),
            ("measure", (self.measure_button,)),
        ):
            for widget in widgets:
                widget.setProperty("actionRole", role)

        root = self.centralWidget()
        root_layout = root.layout()
        root_layout.setContentsMargins(10, 8, 10, 10)
        root_layout.setSpacing(8)
        body = root_layout.itemAt(root_layout.count() - 1).layout()
        if body is None:
            return

        # Keep the native title bar and give the plots all remaining height.
        root_layout.setContentsMargins(2, 2, 2, 2)
        body.setSpacing(2)
        self.setWindowIcon(self.activity_icon("graph"))
        if sys.platform == "win32":
            import ctypes
            hwnd = int(self.winId())
            dark = ctypes.c_int(1)
            caption = ctypes.c_uint(0x0022190F)
            text_color = ctypes.c_uint(0x00F5F0E7)
            try:
                dwm = ctypes.windll.dwmapi
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20, ctypes.byref(dark), 4)
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 35, ctypes.byref(caption), 4)
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 36, ctypes.byref(text_color), 4)
            except (AttributeError, OSError):
                pass

        # The original UI has one large side layout.  Extract its group boxes
        # and place them into independent pages without recreating controls.
        side_scroll = root.findChild(QScrollArea, "SidePanelScroll")
        side_panel = root.findChild(QFrame, "SidePanel")
        splitter = self.splitter
        if side_scroll is None or side_panel is None:
            return
        self.tool_sidebar = side_scroll
        side_scroll.setMinimumWidth(210)
        side_scroll.setMaximumWidth(16777215)
        side_scroll.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        side_panel.setMinimumWidth(0)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        side_layout = side_panel.layout()
        if side_layout is None:
            return
        group_by_title = {
            group.title(): group
            for group in side_panel.findChildren(QGroupBox)
        }
        group_names = (
            "回放控制",
            "超声筛选",
            "显示设置",
            "同步校准",
            "软件更新",
            "信号距离测量",
            "模型异常候选 / 频率提示",
            "人工标注（只标异常）",
            "当前窗口信息",
        )
        groups = {name: group_by_title[name] for name in group_names}
        while side_layout.count():
            item = side_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)

        side_layout.setContentsMargins(12, 10, 12, 10)
        side_layout.setSpacing(8)
        sidebar_header = QFrame()
        sidebar_header.setObjectName("SidebarHeader")
        sidebar_header_layout = QVBoxLayout(sidebar_header)
        sidebar_header_layout.setContentsMargins(2, 1, 2, 8)
        sidebar_header_layout.setSpacing(2)
        self.sidebar_title = QLabel("回放")
        self.sidebar_title.setObjectName("SidebarTitle")
        self.sidebar_hint = QLabel("播放、定位与当前窗口信息")
        self.sidebar_hint.setObjectName("SidebarHint")
        self.sidebar_hint.setWordWrap(True)
        sidebar_header_layout.addWidget(self.sidebar_title)
        sidebar_header_layout.addWidget(self.sidebar_hint)
        side_layout.addWidget(sidebar_header)

        self.tool_stack = QStackedWidget()
        self.tool_stack.setObjectName("ToolStack")
        self.tool_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        side_layout.addWidget(self.tool_stack, 1)

        page_specs = (
            ("回放", "播放、定位与当前窗口信息", (groups["回放控制"], groups["当前窗口信息"])),
            ("标注", "异常框选、编辑与标注跳转", (groups["人工标注（只标异常）"],)),
            ("模型", "模型候选与频率提示", (groups["模型异常候选 / 频率提示"],)),
            ("测量", "选取两个信号，测量沿线路距离", (groups["信号距离测量"],)),
            (
                "设置",
                "筛选、显示参数、同步校准与软件更新",
                (
                    groups["超声筛选"],
                    groups["显示设置"],
                    groups["同步校准"],
                    groups["软件更新"],
                ),
            ),
        )
        self.tool_page_meta = [(title, hint) for title, hint, _ in page_specs]
        for _title, _hint, widgets in page_specs:
            page = QWidget()
            page.setObjectName("ToolPage")
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(1, 2, 1, 8)
            page_layout.setSpacing(10)
            for widget in widgets:
                page_layout.addWidget(widget)
                widget.show()
            page_layout.addStretch(1)
            # Each section scrolls independently; the long settings page
            # must not impose its minimum height on the other sections.
            page_scroll = QScrollArea()
            page_scroll.setWidgetResizable(True)
            page_scroll.setFrameShape(QFrame.NoFrame)
            page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            page_layout.setSizeConstraint(QLayout.SetMinAndMaxSize)
            for label in page.findChildren(QLabel):
                label.setWordWrap(True)
                label.setMinimumWidth(0)
                label.setTextFormat(Qt.PlainText)
            for button in page.findChildren(QPushButton):
                button.setMinimumWidth(button.sizeHint().width())
                button.setMinimumHeight(button.sizeHint().height())
            for combo in page.findChildren(QComboBox):
                combo.setMinimumWidth(100)
                combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
                combo.setMinimumContentsLength(8)
            page_scroll.setWidget(page)
            self.tool_stack.addWidget(page_scroll)

        # Add the narrow activity rail before the sidebar.  It is deliberately
        # made from QToolButtons so it remains keyboard/focus accessible.
        activity_bar = QFrame()
        activity_bar.setObjectName("ActivityBar")
        activity_bar.setFixedWidth(58)
        activity_layout = QVBoxLayout(activity_bar)
        activity_layout.setContentsMargins(6, 8, 6, 8)
        activity_layout.setSpacing(7)
        activity_specs = (
            ("play-circle", "回放", "回放控制与当前窗口信息"),
            ("edit", "标注", "人工标注与复核"),
            ("graph", "模型", "模型候选与频率提示"),
            ("arrow-both", "测量", "信号两点距离测量"),
            ("settings", "设置", "超声筛选、显示参数与同步校准"),
        )
        self.activity_buttons: list[QToolButton] = []
        for index, (icon, name, tip) in enumerate(activity_specs):
            button = QToolButton()
            button.setObjectName("ActivityButton")
            button.setIcon(self.activity_icon(icon))
            button.setIconSize(QSize(24, 24))
            button.setText(name)
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setToolTip(f"{name} · {tip}")
            button.setAccessibleName(name)
            button.setCheckable(True)
            button.setFixedSize(44, 62)
            button.clicked.connect(
                lambda checked=False, page_index=index: self.toggle_tool_page(page_index)
            )
            self.activity_buttons.append(button)
            activity_layout.addWidget(button)
        activity_layout.addStretch(1)
        body.insertWidget(0, activity_bar)

        # No duplicate plot heading: the synchronized canvases fill the body.
        self.us_plot.setMinimumHeight(200)
        self.mfl_plot.setMinimumHeight(400)
        # Horizontal splitter replaces a rigid sidebar width. Oversized tool
        # pages scroll rather than squeezing controls past their text size.
        body.removeWidget(side_scroll)
        body.removeWidget(splitter)
        self.workspace_splitter = QSplitter(Qt.Horizontal)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.addWidget(side_scroll)
        self.plot_scroll = QScrollArea()
        self.plot_scroll.setWidgetResizable(True)
        self.plot_scroll.setFrameShape(QFrame.NoFrame)
        self.plot_scroll.setWidget(splitter)
        self.workspace_splitter.addWidget(self.plot_scroll)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setSizes([360, 850])
        body.addWidget(self.workspace_splitter, 1)

        # Set the initial page only after all activity buttons exist.
        self.set_tool_page(0)

        source_bar = QFrame()
        source_bar.setObjectName("MarkerSourceBar")
        source_layout = QHBoxLayout(source_bar)
        source_layout.setContentsMargins(8, 4, 8, 4)
        source_layout.addWidget(QLabel("标记来源"))
        self.annotation_source_check = QCheckBox("显示")
        self.annotation_source_check.setChecked(True)
        self.annotation_source_check.setAccessibleName("显示人工标注文件")
        self.annotation_source_check.toggled.connect(self.set_annotation_layer_visible)
        self.model_source_check = QCheckBox("显示")
        self.model_source_check.setAccessibleName("显示模型结果文件")
        self.model_source_check.toggled.connect(self.model_result_check.setChecked)
        self.annotation_source_label = QLabel()
        self.model_source_label = QLabel()
        for label in (self.annotation_source_label, self.model_source_label):
            source_layout.addWidget(self.annotation_source_check if label is self.annotation_source_label else self.model_source_check)
            label.setTextFormat(Qt.PlainText)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            label.setWordWrap(True)
            source_layout.addWidget(label, 1)
        source_bar.setStyleSheet(
            "QFrame#MarkerSourceBar { background: #161b22; border-top: 1px solid #30363d; }"
            "QLabel { background: transparent; color: #c9d1d9; font-size: 8pt; }"
        )
        root_layout.addWidget(source_bar)
        self.update_marker_sources()

        side_panel.setStyleSheet("""
            QFrame#SidePanel { background: #161b22; border: 1px solid #30363d; }
            QWidget#ToolPage, QStackedWidget { background: transparent; }
            QGroupBox { background: #21262d; border: 1px solid #3d444d; }
            QGroupBox::title { color: #c9d1d9; }
            QLabel { background: transparent; color: #c9d1d9; }
            QLabel#InfoLabel, QLabel#SidebarHint { color: #9da7b3; }
            QPushButton, QToolButton { background: #30363d; border: 1px solid #484f58; color: #e6edf3; }
            QPushButton:hover, QToolButton:hover { background: #3d444d; border-color: #8b949e; }
            QComboBox, QDoubleSpinBox, QSpinBox, QListWidget { background: #0d1117; color: #e6edf3; border: 1px solid #484f58; }
            QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover { border-color: #58a6ff; }
            QPushButton[actionRole="accent"] { background: #1f6feb; border-color: #388bfd; color: #ffffff; }
            QPushButton[actionRole="accent"]:hover { background: #388bfd; }
            QPushButton[actionRole="success"] { background: #238636; border-color: #3fb950; color: #ffffff; }
            QPushButton[actionRole="success"]:hover { background: #2ea043; }
            QPushButton[actionRole="warning"] { background: #3b2e16; border-color: #9e6a03; color: #f2cc60; }
            QPushButton[actionRole="warning"]:hover { background: #513c17; }
            QPushButton[actionRole="danger"] { background: #3c2025; border-color: #8e3b46; color: #ff9b98; }
            QPushButton[actionRole="danger"]:hover { background: #612b31; }
            QPushButton[actionRole="model"] { background: #33264d; border-color: #8957b1; color: #d2a8ff; }
            QPushButton[actionRole="model"]:hover { background: #493366; }
            QPushButton[actionRole="measure"] { background: #163c40; border-color: #319b9d; color: #7ee0d7; }
            QPushButton[actionRole="measure"]:hover { background: #205456; }
            QPushButton:checked { border: 2px solid #79c0ff; }
            QToolButton:checked { background: #1b3556; color: #a5d6ff; border-color: #388bfd; }
            QPushButton:disabled, QPushButton[actionRole]:disabled, QToolButton:disabled,
            QComboBox:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled {
                background: #20252b; color: #707b88; border: 1px solid #30363d;
            }
            QPushButton:focus, QToolButton:focus, QComboBox:focus,
            QDoubleSpinBox:focus, QSpinBox:focus { border: 2px solid #79c0ff; }
            QListWidget::item:selected { background: #1b3556; color: #a5d6ff; }
            QCheckBox { background: transparent; color: #c9d1d9; }
            QCheckBox::indicator:checked { background: #1f6feb; border-color: #79c0ff; }
            QSlider::handle:horizontal { background: #58a6ff; }
        """)

    @staticmethod
    def activity_icon(name: str) -> QIcon:
        asset_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        svg = (asset_root / "assets" / "codicons" / (name + ".svg")).read_bytes()
        icon = QIcon()
        for mode, state, color in (
            (QIcon.Normal, QIcon.Off, "#95a6b5"),
            (QIcon.Active, QIcon.Off, "#e7f0f5"),
            (QIcon.Normal, QIcon.On, "#69c5ff"),
            (QIcon.Active, QIcon.On, "#69c5ff"),
        ):
            for size in (24, 48):
                renderer = QSvgRenderer(svg.replace(b"currentColor", color.encode("ascii")))
                pixmap = QPixmap(size, size)
                pixmap.fill(Qt.transparent)
                painter = QPainter(pixmap)
                renderer.render(painter)
                painter.end()
                icon.addPixmap(pixmap, mode, state)
        return icon

    def toggle_tool_page(self, index: int) -> None:
        if self.tool_stack.currentIndex() == index and not self.tool_sidebar.isHidden():
            self.tool_sidebar.hide()
            self.activity_buttons[index].setChecked(False)
        else:
            self.tool_sidebar.show()
            self.set_tool_page(index)

    def set_tool_page(self, index: int) -> None:
        """Switch the compact VS Code-style tool sidebar page."""
        if not hasattr(self, "tool_stack"):
            return
        index = max(0, min(index, self.tool_stack.count() - 1))
        self.tool_stack.setCurrentIndex(index)
        if hasattr(self, "tool_page_meta"):
            title, hint = self.tool_page_meta[index]
            self.sidebar_title.setText(title)
            self.sidebar_hint.setText(hint)
            page_colors = ("#79c0ff", "#7ee787", "#d2a8ff", "#7ee0d7", "#e3b341")
            self.sidebar_title.setStyleSheet("color: " + page_colors[index] + "; background: transparent;")
        for button_index, button in enumerate(self.activity_buttons):
            button.setChecked(button_index == index)

    def connect_signals(self) -> None:
        self.choose_dataset_button.clicked.connect(self.choose_dataset)
        self.play_button.clicked.connect(self.toggle_play)
        self.joint_button.clicked.connect(self.locate_joint)
        self.ultrasound_joint_button.clicked.connect(self.locate_ultrasound_joint)
        self.home_button.clicked.connect(lambda: self.set_position(self.data_start))
        self.end_button.clicked.connect(lambda: self.set_position(self.max_start()))
        self.jump_button.clicked.connect(lambda: self.set_position(self.start_spin.value()))
        self.window_combo.currentTextChanged.connect(self.on_window_changed)
        self.speed_combo.currentTextChanged.connect(self.on_speed_changed)
        self.timeline.valueChanged.connect(self.on_timeline_changed)
        self.select_gates_button.clicked.connect(self.select_all_gates)
        self.clear_gates_button.clicked.connect(self.clear_all_gates)
        self.mfl_scale_combo.currentIndexChanged.connect(self.on_mfl_scale_changed)
        self.mfl_background_combo.currentIndexChanged.connect(self.on_mfl_background_changed)
        self.marker_size_combo.valueChanged.connect(self.on_marker_size_changed)
        self.gate_legend_button.clicked.connect(self.show_gate_legend)
        self.us_alignment_spin.valueChanged.connect(self.on_us_alignment_offset_changed)
        self.us_alignment_fine_left_button.clicked.connect(
            lambda: self.nudge_us_alignment(-US_ALIGNMENT_FINE_STEP_M)
        )
        self.us_alignment_fine_right_button.clicked.connect(
            lambda: self.nudge_us_alignment(US_ALIGNMENT_FINE_STEP_M)
        )
        self.us_alignment_coarse_left_button.clicked.connect(
            lambda: self.nudge_us_alignment(-US_ALIGNMENT_COARSE_STEP_M)
        )
        self.us_alignment_coarse_right_button.clicked.connect(
            lambda: self.nudge_us_alignment(US_ALIGNMENT_COARSE_STEP_M)
        )
        self.us_alignment_reset_button.clicked.connect(self.reset_us_alignment)
        self.us_alignment_save_button.clicked.connect(self.save_us_alignment)
        self.annotation_mode_button.toggled.connect(
            self.on_annotation_mode_toggled
        )
        self.annotation_save_button.clicked.connect(self.save_annotation)
        self.annotation_clear_button.clicked.connect(
            self.clear_annotation_selection
        )
        self.annotation_reselect_button.clicked.connect(
            self.begin_annotation_reselection
        )
        self.annotation_jump_combo.currentIndexChanged.connect(
            self.on_annotation_jump_filter_changed
        )
        self.annotation_jump_previous_button.clicked.connect(
            self.jump_to_previous_annotation
        )
        self.annotation_jump_next_button.clicked.connect(
            self.jump_to_next_annotation
        )
        self.annotation_delete_button.clicked.connect(
            self.delete_selected_annotation
        )
        self.annotation_list.currentRowChanged.connect(
            self.on_annotation_row_changed
        )
        self.annotation_list.itemClicked.connect(
            self.on_annotation_item_clicked
        )
        self.model_result_check.toggled.connect(
            self.on_model_result_visibility_changed
        )
        self.model_result_filter_combo.currentIndexChanged.connect(
            self.on_model_result_filter_changed
        )
        self.model_result_previous_button.clicked.connect(
            lambda: self.jump_to_model_prediction(-1)
        )
        self.model_result_next_button.clicked.connect(
            lambda: self.jump_to_model_prediction(1)
        )
        self.model_result_reload_button.clicked.connect(
            lambda: self.load_model_results()
        )
        self.model_result_open_button.clicked.connect(
            self.choose_model_result_file
        )
        self.update_save_repository_button.clicked.connect(
            lambda: self.save_update_repository(show_success=True)
        )
        self.update_check_button.clicked.connect(self.check_for_updates)

    def save_update_repository(self, *, show_success: bool) -> bool:
        try:
            repository = validate_repository(self.update_repository_edit.text())
            _write_json_config(
                self.update_settings_path,
                {"github_repo": repository},
            )
        except (UpdateError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "更新设置无效", str(exc))
            return False
        self.update_repository = repository
        self.update_repository_edit.setText(repository)
        self.update_status_label.setText(f"更新源：github.com/{repository}")
        if show_success:
            QMessageBox.information(self, "更新设置已保存", "GitHub 仓库地址已保存。")
        return True

    def check_for_updates(self) -> None:
        if (
            self.update_check_worker is not None
            and self.update_check_worker.isRunning()
        ) or (
            self.update_download_worker is not None
            and self.update_download_worker.isRunning()
        ):
            return
        if not self.save_update_repository(show_success=False):
            self.update_repository_edit.setFocus()
            return

        self.update_check_button.setEnabled(False)
        self.update_save_repository_button.setEnabled(False)
        self.update_status_label.setText("正在查询 GitHub Release…")
        worker = ReleaseCheckWorker(self.update_repository, APP_VERSION)
        worker.succeeded.connect(self.on_update_check_succeeded)
        worker.failed.connect(self.on_update_check_failed)
        self.update_check_worker = worker
        worker.start()

    def on_update_check_succeeded(self, release: dict[str, Any]) -> None:
        self.update_check_button.setEnabled(True)
        self.update_save_repository_button.setEnabled(True)
        self.latest_release = dict(release)
        checked_at = time.strftime("%H:%M:%S")
        if not release.get("update_available"):
            self.update_status_label.setText(
                f"{checked_at} 检查完成：当前已是最新版本"
                f"（GitHub：{release.get('tag', '未知')}）。"
            )
            QMessageBox.information(
                self,
                "检查更新",
                f"当前版本 {APP_VERSION} 已是最新版本。",
            )
            return

        tag = str(release.get("tag", "未知版本"))
        notes = str(release.get("body") or "").strip()
        if len(notes) > 2400:
            notes = notes[:2400].rstrip() + "…"
        prompt = (
            f"发现新版本：{tag}\n当前版本：{APP_VERSION}\n\n"
            f"{notes or '此版本没有填写更新说明。'}\n\n"
            "更新时软件会关闭并在安装后重新启动。请先保存尚未保存的标注。"
        )
        if not getattr(sys, "frozen", False) or sys.platform != "win32":
            self.update_status_label.setText(
                f"{checked_at} 检查完成：发现新版本 {tag}；"
                "当前运行方式不支持自动替换程序文件。"
            )
            release_url = str(release.get("html_url") or "")
            if release_url:
                prompt += f"\n\n请在 Windows 便携版中更新，或手动打开：\n{release_url}"
            QMessageBox.information(self, "发现新版本", prompt)
            return

        answer = QMessageBox.question(
            self,
            "发现新版本",
            prompt + "\n\n现在下载并安装吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            self.update_status_label.setText(
                f"{checked_at} 已发现新版本 {tag}，暂未更新。"
            )
            return
        self.start_update_download(release)

    def on_update_check_failed(self, message: str) -> None:
        self.update_check_button.setEnabled(True)
        self.update_save_repository_button.setEnabled(True)
        checked_at = time.strftime("%H:%M:%S")
        self.update_status_label.setText(f"{checked_at} {message}")
        QMessageBox.warning(self, "检查更新失败", message)

    def start_update_download(self, release: dict[str, Any]) -> None:
        self.update_check_button.setEnabled(False)
        self.update_save_repository_button.setEnabled(False)
        self.update_progress_bar.setRange(0, 1000)
        self.update_progress_bar.setValue(0)
        self.update_progress_bar.setFormat("准备下载更新包…")
        self.update_progress_bar.setVisible(True)
        self.update_status_label.setText(
            f"正在下载 {release.get('tag', '新版本')}…"
        )
        worker = ReleaseDownloadWorker(release)
        worker.progress.connect(self.on_update_download_progress)
        worker.succeeded.connect(self.on_update_download_succeeded)
        worker.failed.connect(self.on_update_download_failed)
        self.update_download_worker = worker
        worker.start()

    def on_update_download_progress(self, received: int, total: int) -> None:
        if total <= 0:
            self.update_progress_bar.setRange(0, 0)
            self.update_progress_bar.setFormat("正在下载更新包…")
            return
        self.update_progress_bar.setRange(0, 1000)
        self.update_progress_bar.setValue(
            max(0, min(1000, round(received * 1000 / total)))
        )
        self.update_progress_bar.setFormat(
            f"下载更新包：{received / (1024 * 1024):.1f} / "
            f"{total / (1024 * 1024):.1f} MB（%p%）"
        )

    def on_update_download_succeeded(self, archive_name: str) -> None:
        self.update_check_button.setEnabled(True)
        self.update_save_repository_button.setEnabled(True)
        archive_path = Path(archive_name)
        try:
            validate_update_archive(archive_path)
            start_windows_update_installer(archive_path)
        except (UpdateError, OSError) as exc:
            self.update_progress_bar.setVisible(False)
            self.update_status_label.setText(f"更新未安装：{exc}")
            QMessageBox.warning(self, "更新未安装", str(exc))
            try:
                archive_path.unlink(missing_ok=True)
            except OSError:
                pass
            return

        self.update_status_label.setText("更新包已校验，软件即将关闭并重新启动。")
        QMessageBox.information(
            self,
            "正在安装更新",
            "更新包校验完成。软件将关闭，替换程序文件后自动重新启动。",
        )
        self.close()

    def on_update_download_failed(self, message: str) -> None:
        self.update_check_button.setEnabled(True)
        self.update_save_repository_button.setEnabled(True)
        self.update_progress_bar.setVisible(False)
        self.update_status_label.setText(f"下载失败：{message}")
        QMessageBox.warning(self, "下载更新失败", message)

    def _data_control_widgets(self) -> tuple[QWidget, ...]:
        return (
            self.play_button,
            self.joint_button,
            self.ultrasound_joint_button,
            self.home_button,
            self.end_button,
            self.window_combo,
            self.speed_combo,
            self.start_spin,
            self.jump_button,
            self.timeline,
            self.back_100_button,
            self.forward_100_button,
            self.select_gates_button,
            self.clear_gates_button,
            self.mfl_scale_combo,
            self.mfl_background_combo,
            self.marker_size_combo,
            self.gate_legend_button,
            self.us_alignment_spin,
            self.us_alignment_fine_left_button,
            self.us_alignment_fine_right_button,
            self.us_alignment_coarse_left_button,
            self.us_alignment_coarse_right_button,
            self.us_alignment_reset_button,
            self.us_alignment_save_button,
            self.annotation_mode_button,
            self.annotation_label_combo,
            self.annotation_scope_combo,
            self.annotation_direction_combo,
            self.annotation_save_button,
            self.annotation_clear_button,
            self.annotation_reselect_button,
            self.annotation_jump_combo,
            self.annotation_jump_previous_button,
            self.annotation_jump_next_button,
            self.annotation_delete_button,
            self.model_result_check,
            self.model_result_filter_combo,
            self.model_result_previous_button,
            self.model_result_next_button,
            self.model_result_reload_button,
            self.model_result_open_button,
        )

    def _reset_for_dataset_load(self, dataset_name: str) -> None:
        self.request_timer.stop()
        self.prefetch_timer.stop()
        self.cursor_timer.stop()
        self.play_timer.stop()
        self.playing = False
        self.play_button.setText("▶ 开始回放")
        self.fetch_pending = False
        self.manifest = None
        self.data = None
        self.joint_detection_status = ""
        self.us_joint_anchor_label.setText("超声配置接头 ID：读取中…")
        self.joint_label.setText("\u672a\u5b9a\u4f4d\uff08\u70b9\u51fb\u4e0a\u65b9\u6309\u94ae\uff09")
        self.window_cache.clear()
        self.request_key_by_serial.clear()
        self.pending_request_keys.clear()
        self.prefetch_serials.clear()
        self.request_serial += 1
        self.cursor_request_serial += 1
        self.cursor_x = None
        self.cursor_sample = None
        self.desired_x_start = 0.0
        self.view_x_start = 0.0
        self.annotation_mode = False
        self.annotation_drag_start_x = None
        self.annotation_drag_current_x = None
        self.annotation_selection_start_m = None
        self.annotation_selection_end_m = None
        self.annotation_edit_id = None
        self.annotation_reselecting = False
        self.annotation_mode_button.blockSignals(True)
        self.annotation_mode_button.setChecked(False)
        self.annotation_mode_button.blockSignals(False)
        self.annotation_list.clear()
        self.annotation_jump_combo.blockSignals(True)
        self.annotation_jump_combo.setCurrentIndex(0)
        self.annotation_jump_combo.blockSignals(False)
        self.model_result_filter_combo.blockSignals(True)
        self.model_result_filter_combo.setCurrentIndex(0)
        self.model_result_filter_combo.blockSignals(False)
        self.model_result_check.blockSignals(True)
        self.model_result_check.setChecked(False)
        self.model_result_check.blockSignals(False)
        self.model_result_path = None
        self.model_predictions = []
        self.model_result_error = ""
        self.model_result_selected_id = None
        self.model_result_enabled = False
        self.default_us_alignment_offset_m = self.read_us_alignment_offset()
        self.us_alignment_offset_m = self.default_us_alignment_offset_m
        self.us_alignment_dirty = False
        self.us_alignment_spin.blockSignals(True)
        self.us_alignment_spin.setValue(self.us_alignment_offset_m)
        self.us_alignment_spin.blockSignals(False)
        self.range_label.setText("范围：等待数据")
        self.data_label.setText(f"数据集：{dataset_name}\n等待加载")
        self.server_label.setText("状态：正在切换数据…")
        self.health_label.setText("正在加载数据…")
        self.load_progress.setValue(0)
        self.load_progress.setFormat("加载中 0.0%")
        self.annotation_selection_label.setText("选中区间：未选择")
        self.annotation_status_label.setText("正在读取当前数据的人工标注…")
        self.model_result_status_label.setText("正在读取模型结果…")
        self.update_annotation_edit_ui()
        self.build_gate_buttons()
        self._set_data_controls_enabled(False)
        self.update_marker_sources()
        self.us_plot.update()
        self.mfl_plot.update()

    def _set_data_controls_enabled(self, enabled: bool) -> None:
        for widget in self._data_control_widgets():
            widget.setEnabled(bool(enabled))

    def choose_dataset(self) -> None:
        start_dir = self.dataset_root if self.dataset_root.is_dir() else self.config_path.parent
        selected_name = QFileDialog.getExistingDirectory(
            self,
            "选择数据目录",
            str(start_dir),
        )
        if not selected_name:
            return
        try:
            dataset_config, dataset_name, dataset_root = _prepare_dataset_selection(
                Path(selected_name),
                self.config_path,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "数据目录不可用", str(exc))
            return

        self._load_dataset_config(dataset_config, dataset_name, dataset_root)

    def _load_dataset_config(
        self,
        config_path: Path,
        dataset_name: str,
        dataset_root: Path,
    ) -> None:
        if self._stopping_worker is not None or self._pending_dataset_load is not None:
            return

        config_path = Path(config_path).resolve()
        try:
            # Validate the new configuration before stopping the current worker.
            # This prevents a bad selection from leaving the window without a
            # usable data source.
            annotation_store = AnnotationStore(config_path)
            annotation_groups = annotation_store.load_event_groups()
        except Exception as exc:
            QMessageBox.warning(self, "数据目录不可用", str(exc))
            return

        self._pending_dataset_load = (
            config_path,
            str(dataset_name),
            Path(dataset_root).resolve(),
            annotation_store,
            annotation_groups,
        )
        old_worker = self.worker
        self._stopping_worker = old_worker
        self.choose_dataset_button.setEnabled(False)
        self._set_joint_search_buttons_enabled(False)
        self.load_progress.setValue(0)
        self.load_progress.setFormat("等待切换 0.0%")
        self.data_label.setText(f"数据集：{dataset_name}\n准备加载")
        self.server_label.setText(f"状态：已选择 {dataset_name}，正在切换…")
        self.health_label.setText("当前数据仍在加载，正在等待后台安全退出…")
        old_worker.request_stop()
        QTimer.singleShot(0, self._finish_dataset_load_when_stopped)

    def _finish_dataset_load_when_stopped(self) -> None:
        """Switch datasets only after the previous QThread has really stopped."""
        old_worker = self._stopping_worker
        pending = self._pending_dataset_load
        if old_worker is None or pending is None:
            return
        if old_worker.isRunning():
            # Loading a large DAT set can take longer than a normal UI event.
            # Keep the window responsive and poll instead of destroying a live
            # QThread (which causes an immediate Qt process abort).
            QTimer.singleShot(50, self._finish_dataset_load_when_stopped)
            return

        old_worker.deleteLater()
        self._stopping_worker = None
        self._pending_dataset_load = None
        (
            config_path,
            dataset_name,
            dataset_root,
            annotation_store,
            annotation_groups,
        ) = pending

        try:
            self.config_path = config_path
            self.dataset_name = dataset_name
            self.dataset_root = dataset_root
            self.annotation_store = annotation_store
            self.annotation_groups = annotation_groups
            self._reset_for_dataset_load(dataset_name)
            self.setWindowTitle(f"超声 · 漏磁同步回放  |  {dataset_name}")

            self.worker = DataWorker(self.config_path)
            self._connect_worker(self.worker)
            self.worker.start()
        except Exception as exc:
            self.choose_dataset_button.setEnabled(True)
            QMessageBox.critical(self, "加载数据失败", str(exc))

    @property
    def data_start(self) -> float:
        return float(self.manifest.get("range", {}).get("startM", -1.0)) if self.manifest else -1.0

    @property
    def data_end(self) -> float:
        return float(self.manifest.get("range", {}).get("endM", 1.0)) if self.manifest else 1.0

    def view_range(self) -> tuple[float, float]:
        return self.view_x_start, self.view_x_start + self.window_width

    def max_start(self) -> float:
        return max(self.data_start, self.data_end - self.window_width)

    def selected_tracks(self) -> list[int]:
        return sorted(
            track
            for track, checkbox in self.track_boxes.items()
            if checkbox.isChecked()
        )

    def selected_gates(self) -> Optional[list[int]]:
        if not self.gate_buttons:
            return None
        return sorted(
            gate for gate, button in self.gate_buttons.items() if button.isChecked()
        )

    def on_ready(self, manifest: dict[str, Any]) -> None:
        self.choose_dataset_button.setEnabled(True)
        self.load_progress.setValue(950)
        self.load_progress.setFormat("正在读取首屏 95%")
        self.manifest = manifest
        configured_us_id = manifest.get("joint", {}).get("usId")
        if configured_us_id is not None:
            self.us_joint_anchor_label.setText(
                f"超声配置接头 ID：{int(configured_us_id):,}"
            )
        self.setWindowTitle(f"超声 · 漏磁同步回放  |  {self.dataset_name}")
        self.data_label.setText(f"数据集：{self.dataset_name}\n正在读取回放窗口…")
        self.window_width = float(self.window_combo.currentText().split()[0])
        self.desired_x_start = clamp(-self.window_width / 2, self.data_start, self.max_start())
        self.view_x_start = self.desired_x_start
        self.start_spin.setValue(self.desired_x_start)

        for widget in (
            self.play_button,
            self.joint_button,
            self.ultrasound_joint_button,
            self.home_button,
            self.end_button,
            self.window_combo,
            self.speed_combo,
            self.start_spin,
            self.jump_button,
            self.timeline,
            self.back_100_button,
            self.forward_100_button,
            self.select_gates_button,
            self.clear_gates_button,
            self.mfl_scale_combo,
            self.mfl_background_combo,
            self.marker_size_combo,
            self.gate_legend_button,
            self.us_alignment_spin,
            self.us_alignment_fine_left_button,
            self.us_alignment_fine_right_button,
            self.us_alignment_coarse_left_button,
            self.us_alignment_coarse_right_button,
            self.us_alignment_reset_button,
            self.us_alignment_save_button,
            self.annotation_mode_button,
            self.annotation_label_combo,
            self.annotation_scope_combo,
            self.annotation_direction_combo,
            self.annotation_save_button,
            self.annotation_clear_button,
            self.annotation_reselect_button,
            self.annotation_jump_combo,
            self.annotation_jump_previous_button,
            self.annotation_jump_next_button,
            self.annotation_delete_button,
            self.model_result_check,
            self.model_result_filter_combo,
            self.model_result_previous_button,
            self.model_result_next_button,
            self.model_result_reload_button,
            self.model_result_open_button,
        ):
            widget.setEnabled(True)
        self._set_joint_search_buttons_enabled(True)

        self.build_gate_buttons()
        self.load_model_results()
        self.refresh_annotation_list()
        self.update_us_alignment_status()
        mfl = manifest.get("mfl", {})
        ultrasound = manifest.get("ultrasound", {})
        warning_count = len(manifest.get("warnings", []))
        self.health_label.setText(
            "● 已加载 · MFL 4段 / 16行 · US "
            + format_count(ultrasound.get("totalRecords", 0))
            + (" · 有提示" if warning_count else "")
        )
        self.server_label.setText("\u72b6\u6001\uff1a\u7d22\u5f15\u5df2\u5c31\u7eea\uff0c\u6b63\u5728\u8bfb\u53d6\u9996\u5c4f")
        self.update_timeline()
        self.request_window()

    def set_annotation_layer_visible(self, visible: bool) -> None:
        self.annotation_layer_visible = visible
        self.update_marker_sources()
        self.us_plot.update()
        self.mfl_plot.update()

    def update_marker_sources(self) -> None:
        """Keep loaded marker files visible even when the tool sidebar is closed."""
        if not hasattr(self, "annotation_source_label"):
            return
        annotation_path = self.annotation_store.event_path.resolve()
        self.model_source_check.blockSignals(True)
        self.model_source_check.setChecked(self.model_result_check.isChecked())
        self.model_source_check.setEnabled(self.model_result_check.isEnabled())
        self.model_source_check.blockSignals(False)
        annotation_status = (
            f"{len(self.annotation_groups)} 条" if annotation_path.exists() else "尚未创建"
        )
        if not self.annotation_layer_visible:
            annotation_status += " · 已隐藏"
        self.annotation_source_label.setText(
            f"人工标注：{annotation_path.name} · {annotation_status}"
        )
        self.annotation_source_label.setToolTip(
            f"人工标注文件（漏磁图）\n{annotation_path}\n{annotation_status}"
        )
        path = self.model_result_path
        if self.model_result_error:
            status = "加载失败"
        elif path is None:
            status = "未加载"
        elif not self.model_predictions:
            status = "已加载 · 0 条"
        elif not self.model_result_enabled:
            status = f"{len(self.model_predictions)} 条 · 图层已关闭"
        else:
            status = f"{len(self.model_predictions)} 条 · 筛选后 {len(self.model_result_targets())} 条"
        self.model_source_label.setText(
            f"模型：{path.name if path else '无结果文件'} · {status}"
        )
        self.model_source_label.setToolTip(
            f"模型结果文件（超声 / 漏磁图）\n{path or '未加载文件'}\n{status}"
            + (f"\n{self.model_result_error}" if self.model_result_error else "")
        )

    def load_model_results(self, path: Optional[Path] = None) -> None:
        """Load model events without changing the human annotation store."""
        if self.worker.service is None:
            return
        configured_path, default_segment = self.read_model_result_config()
        result_path = Path(path).resolve() if path is not None else configured_path
        self.model_result_path = result_path
        self.model_predictions = []
        self.model_result_error = ""
        requested_visible = bool(self.model_result_check.isChecked())

        if result_path is None:
            self.model_result_check.blockSignals(True)
            self.model_result_check.setChecked(False)
            self.model_result_check.blockSignals(False)
            self.model_result_enabled = False
            self.model_result_check.setEnabled(False)
            self.model_result_filter_combo.setEnabled(False)
            self.model_result_previous_button.setEnabled(False)
            self.model_result_next_button.setEnabled(False)
            self.model_result_status_label.setText("未配置模型结果文件")
            self.update_marker_sources()
            self.model_result_detail_label.setText(
                "可在 config.json 中配置 model_result_path，或选择一个模型 CSV。"
            )
            self.us_plot.update()
            self.mfl_plot.update()
            return

        try:
            store = ModelResultStore(
                result_path,
                self.worker.service.mfl,
                default_segment=default_segment,
            )
            self.model_predictions = store.predictions
            self.model_result_enabled = requested_visible and bool(
                self.model_predictions
            )
            self.model_result_check.blockSignals(True)
            self.model_result_check.setChecked(self.model_result_enabled)
            self.model_result_check.blockSignals(False)
            self.model_result_check.setEnabled(bool(self.model_predictions))
            self.model_result_filter_combo.setEnabled(bool(self.model_predictions))
            self.model_result_reload_button.setEnabled(True)
            self.model_result_open_button.setEnabled(True)
            anomaly_count = sum(
                1 for item in self.model_predictions if item.get("is_anomaly")
            )
            frequency_hint_count = sum(
                1 for item in self.model_predictions if item.get("frequency_hint")
            )
            hint_suffix = (
                f"含 {frequency_hint_count:,} 条频率提示；"
                if frequency_hint_count
                else ""
            )
            self.model_result_detail_label.setText(
                f"{result_path.name} · {store.format_name} · "
                f"模型异常 {anomaly_count:,} / 总结果 {len(self.model_predictions):,}。"
                + hint_suffix
                + "频率提示仅用于辅助复核，人工确认后再保存为标注。"
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.model_result_error = str(exc)
            self.model_result_check.blockSignals(True)
            self.model_result_check.setChecked(False)
            self.model_result_check.blockSignals(False)
            self.model_result_enabled = False
            self.model_result_check.setEnabled(False)
            self.model_result_filter_combo.setEnabled(False)
            self.model_result_previous_button.setEnabled(False)
            self.model_result_next_button.setEnabled(False)
            self.model_result_reload_button.setEnabled(True)
            self.model_result_open_button.setEnabled(True)
            self.model_result_status_label.setText("模型结果加载失败：" + str(exc))
            self.update_marker_sources()
            self.model_result_detail_label.setText(
                f"文件：{result_path}\n可重新加载或选择其他 CSV 结果文件。"
            )
            self.us_plot.update()
            self.mfl_plot.update()
            return

        self.model_result_filter = str(
            self.model_result_filter_combo.currentData()
        )
        self.model_result_selected_id = None
        self.update_model_result_controls()
        self.us_plot.update()
        self.mfl_plot.update()

    def choose_model_result_file(self) -> None:
        configured_path, _ = self.read_model_result_config()
        start_dir = (
            configured_path.parent
            if configured_path is not None and configured_path.parent.exists()
            else self.config_path.parent
        )
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "选择模型结果 CSV",
            str(start_dir),
            "CSV 文件 (*.csv);;所有文件 (*.*)",
        )
        if filename:
            self.load_model_results(Path(filename))

    def model_result_targets(self) -> list[dict[str, Any]]:
        if (
            not self.model_result_enabled
            or not self.model_result_check.isChecked()
        ):
            return []
        filter_code = self.model_result_filter
        if hasattr(self, "model_result_filter_combo"):
            current_data = self.model_result_filter_combo.currentData()
            if current_data is not None:
                filter_code = str(current_data)
        if filter_code == "anomaly":
            return [
                item for item in self.model_predictions if item.get("is_anomaly")
            ]
        if filter_code in {"left", "right"}:
            return [
                item
                for item in self.model_predictions
                if str(item.get("scope", "all")) == filter_code
            ]
        if filter_code.startswith("frequency_hint:"):
            hint = filter_code.split(":", 1)[1]
            return [
                item
                for item in self.model_predictions
                if str(item.get("frequency_hint", "")) == hint
            ]
        if not filter_code:
            return list(self.model_predictions)
        return [
            item
            for item in self.model_predictions
            if str(item.get("label", "")) == filter_code
        ]

    def update_model_result_controls(self) -> None:
        self.update_marker_sources()
        targets = self.model_result_targets()
        count = len(targets)
        selected_index = next(
            (
                index
                for index, item in enumerate(targets)
                if str(item.get("prediction_id", ""))
                == str(self.model_result_selected_id)
            ),
            -1,
        )
        enabled = bool(targets) and self.manifest is not None
        self.model_result_previous_button.setEnabled(enabled)
        self.model_result_next_button.setEnabled(enabled)
        if not self.model_predictions:
            self.model_result_status_label.setText(
                "未加载模型结果" if not self.model_result_error else "加载失败"
            )
        elif not targets:
            self.model_result_status_label.setText("当前筛选条件下共 0 处")
        elif selected_index >= 0:
            self.model_result_status_label.setText(
                f"当前筛选：{selected_index + 1} / {count}"
            )
        else:
            self.model_result_status_label.setText(f"当前筛选：共 {count:,} 处")

    def on_model_result_visibility_changed(self, checked: bool) -> None:
        self.model_result_enabled = bool(checked)
        if not checked:
            self.model_result_selected_id = None
        self.update_model_result_controls()
        self.us_plot.update()
        self.mfl_plot.update()

    def on_model_result_filter_changed(self, _index: int) -> None:
        current_data = self.model_result_filter_combo.currentData()
        self.model_result_filter = (
            str(current_data) if current_data is not None else "anomaly"
        )
        self.model_result_selected_id = None
        self.update_model_result_controls()
        self.us_plot.update()
        self.mfl_plot.update()

    def model_prediction_at_x(
        self,
        x_value: float,
    ) -> Optional[dict[str, Any]]:
        targets = self.model_result_targets()
        if not targets:
            return None
        try:
            plot_width = max(1.0, float(self.mfl_plot.geometry_info()["plot_width"]))
            tolerance = max(0.002, self.window_width / plot_width * 3.0)
        except (AttributeError, TypeError, ValueError):
            tolerance = 0.01
        candidates = [
            item
            for item in targets
            if float(item.get("x_start_m", 0.0)) - tolerance
            <= float(x_value)
            <= float(item.get("x_end_m", 0.0)) + tolerance
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (
                max(
                    0.0,
                    float(item.get("x_end_m", 0.0))
                    - float(item.get("x_start_m", 0.0)),
                ),
                str(item.get("prediction_id", "")),
            ),
        )

    @staticmethod
    def model_scope_name(scope: str) -> str:
        return {
            "left": "左侧",
            "right": "右侧",
            "all": "左右两侧",
        }.get(scope, scope or "左右两侧")

    def model_prediction_detail(self, prediction: dict[str, Any]) -> str:
        source_row = prediction.get("source_row", {})
        scope = self.model_scope_name(str(prediction.get("scope", "all")))
        start = float(prediction.get("x_start_m", 0.0))
        end = float(prediction.get("x_end_m", 0.0))
        detail = (
            f"{prediction.get('prediction_id', '')} · "
            f"{prediction.get('label_name', '模型异常')} · {scope}\n"
            f"区间：{format_distance(start)} ~ {format_distance(end)}"
            f"（宽 {self.distance_text(end - start)}）"
        )
        detail += f"\n异常分数：{float(prediction.get('score', 0.0)):.3f}"
        record_text = (
            f"\nDAT record：{prediction.get('start_record', '—')}"
            f" ~ {prediction.get('end_record', '—')}"
        )
        detail += record_text
        frequency_hint = str(prediction.get("frequency_hint", "")).strip()
        if frequency_hint:
            frequency_name = str(
                prediction.get("frequency_hint_name", frequency_hint)
            )
            hint_kind = "形态提示" if source_row.get("method") in {"xz_opposition", "xz_template_match"} else "频率提示"
            detail += f"\n{hint_kind}（暂定）：{frequency_name}"
            if source_row.get("method") == "xz_template_match":
                detail += (
                    f"\n命中通道：{source_row.get('channels', '—')}；代表通道：{source_row.get('representative_channel', '—')}"
                    f"\n参考专家标注：{source_row.get('template_id', '—')}"
                    f"\nX/Z联合形态匹配：{float(source_row.get('template_similarity', 0)):.3f}（非概率）"
                    f"\n局部X/Z峰峰值：{float(source_row.get('amplitude_x', 0)):.3f} / {float(source_row.get('amplitude_z', 0)):.3f}"
                )
            if source_row.get("method") == "xz_opposition":
                detail += (
                    f"\n命中通道：{source_row.get('channels', '—')}"
                    f"；代表通道：{source_row.get('representative_channel', '—')}"
                    f"\n反向变化指标：{float(source_row.get('cosine', 0)):.3f}（越接近-1越反向）"
                    f"\nX/Z峰峰值：{float(source_row.get('amplitude_x', 0)):.3f} / {float(source_row.get('amplitude_z', 0)):.3f}"
                    f"\nX/Z波动与背景比：{float(source_row.get('rms_ratio_x', 0)):.2f} / {float(source_row.get('rms_ratio_z', 0)):.2f}"
                )
            if source_row.get("method") == "spatial_band_energy_ratio":
                detail += (
                    f"\n20～50周期/米能量：X {float(source_row['high_ratio_x']):.2f}倍"
                    f" / Z {float(source_row['high_ratio_z']):.2f}倍"
                    f"\n0～5周期/米能量：X {float(source_row['low_ratio_x']):.2f}倍"
                    f" / Z {float(source_row['low_ratio_z']):.2f}倍"
                )
            elif source_row.get("frequency_peak_cpr"):
                detail += (
                    f" · 主频 {float(prediction.get('frequency_peak_cpr', 0.0)):.6f}"
                    " 周期/记录"
                )
            direction = str(
                prediction.get("frequency_change_direction", "")
            ).strip()
            if direction:
                detail += "\n相对邻域：" + (
                    "高于" if direction == "higher_than_background" else "低于"
                )
        reason = str(prediction.get("reason", "")).strip()
        if reason:
            detail += "\n规则/原因：" + reason[:120]
        return detail

    def select_model_prediction(
        self,
        prediction: dict[str, Any],
        *,
        center: bool = False,
    ) -> None:
        self.model_result_selected_id = str(prediction.get("prediction_id", ""))
        self.model_result_detail_label.setText(
            self.model_prediction_detail(prediction)
        )
        if center:
            self.center_on(
                (
                    float(prediction.get("x_start_m", 0.0))
                    + float(prediction.get("x_end_m", 0.0))
                )
                / 2.0
            )
        self.update_model_result_controls()
        self.us_plot.update()
        self.mfl_plot.update()

    def jump_to_model_prediction(self, direction: int) -> None:
        if not self.manifest:
            return
        targets = self.model_result_targets()
        if not targets:
            self.model_result_status_label.setText("当前筛选条件下共 0 处")
            return
        current_index = next(
            (
                index
                for index, item in enumerate(targets)
                if str(item.get("prediction_id", ""))
                == str(self.model_result_selected_id)
            ),
            -1,
        )
        if current_index >= 0:
            target_index = (current_index + (1 if direction > 0 else -1)) % len(targets)
        else:
            view_start, view_end = self.view_range()
            if direction > 0:
                following = [
                    index
                    for index, item in enumerate(targets)
                    if float(item.get("x_start_m", 0.0)) > view_end
                ]
                target_index = following[0] if following else 0
            else:
                preceding = [
                    index
                    for index, item in enumerate(targets)
                    if float(item.get("x_end_m", 0.0)) < view_start
                ]
                target_index = preceding[-1] if preceding else len(targets) - 1
        self.select_model_prediction(targets[target_index], center=True)

    def build_gate_buttons(self) -> None:
        while self.gate_layout.count():
            item = self.gate_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.gate_buttons.clear()
        gates = self.manifest.get("ultrasound", {}).get("gates", []) if self.manifest else []
        for index, item in enumerate(gates):
            gate = int(item.get("gate", 0))
            button = QToolButton()
            button.setText(f"G{gate}  {format_count(item.get('count', 0))}")
            button.setCheckable(True)
            button.setChecked(True)
            button.setEnabled(True)
            shape, color, angle = gate_style(gate)
            shape_name = GATE_SHAPE_NAMES.get(shape, shape)
            name = GATE_NAMES.get(gate, item.get("name", f"Gate {gate}"))
            button.setToolTip(
                f"{name} | {shape_name} | {GATE_COLOR_NAMES.get(str(color).upper(), color)} | angle={angle:+g} deg"
            )
            button.setMinimumHeight(28)
            button.clicked.connect(self.schedule_request)
            self.gate_layout.addWidget(button, index // 2, index % 2)
            self.gate_buttons[gate] = button

    def on_window_changed(self, text: str) -> None:
        try:
            self.window_width = float(text.split()[0])
        except (TypeError, ValueError):
            self.window_width = 6.0
        self.set_position(self.desired_x_start)

    def on_speed_changed(self, text: str) -> None:
        try:
            self.speed = float(text.split()[0])
        except (TypeError, ValueError):
            self.speed = 5.0

    def on_mfl_scale_changed(self, _index: int) -> None:
        mode = self.mfl_scale_combo.currentData()
        self.mfl_scale_mode = "auto" if mode is None else mode
        self.update_mfl_scale()
        self.mfl_plot.update()

    def on_mfl_background_changed(self, _index: int) -> None:
        mode = self.mfl_background_combo.currentData()
        self.mfl_background_mode = mode if mode in MFL_THEMES else "dark"
        self.mfl_plot.update()

    def on_marker_size_changed(self, _index: int) -> None:
        size = self.marker_size_combo.value()
        try:
            self.ultrasound_marker_size = float(size)
        except (TypeError, ValueError):
            self.ultrasound_marker_size = 8.0
        self.us_plot.update()

    def update_us_alignment_status(self) -> None:
        offset = self.us_alignment_offset_m
        sign_text = "右移" if offset >= 0 else "左移"
        self.us_alignment_hint.setText(
            "以漏磁为基准；当前超声%s %.3f m。正值=超声向右移，负值=向左移。"
            "调整只改变显示坐标，不修改原始数据。"
            % (sign_text, abs(offset))
        )

    def on_us_alignment_offset_changed(self, value: float) -> None:
        self.us_alignment_offset_m = clamp(
            float(value),
            -US_ALIGNMENT_OFFSET_LIMIT_M,
            US_ALIGNMENT_OFFSET_LIMIT_M,
        )
        self.us_alignment_dirty = (
            abs(
                self.us_alignment_offset_m
                - self.default_us_alignment_offset_m
            )
            > 0.0000005
        )
        self.update_us_alignment_status()
        # The raw payload/cache remains valid; only the ultrasound x mapping
        # changes, so both the current plot and future cached windows update
        # without a new disk read.
        self.us_plot.update()

    def nudge_us_alignment(self, delta_m: float) -> None:
        if not self.manifest:
            return
        target = clamp(
            self.us_alignment_offset_m + float(delta_m),
            -US_ALIGNMENT_OFFSET_LIMIT_M,
            US_ALIGNMENT_OFFSET_LIMIT_M,
        )
        self.us_alignment_spin.setValue(target)

    def reset_us_alignment(self) -> None:
        self.us_alignment_spin.setValue(self.default_us_alignment_offset_m)

    def save_us_alignment(self) -> None:
        """Persist only the display calibration in the selected config file."""
        try:
            config = json.loads(
                self.config_path.read_text(encoding="utf-8-sig")
            )
            alignment = config.setdefault("alignment", {})
            alignment["ultrasound_offset_m"] = round(
                float(self.us_alignment_offset_m),
                3,
            )
            _write_json_config(self.config_path, config)
            self.default_us_alignment_offset_m = self.us_alignment_offset_m
            self.us_alignment_dirty = False
            self.update_us_alignment_status()
            self.server_label.setText(
                "校准已保存：超声相对偏移 "
                + f"{self.us_alignment_offset_m:+.3f} m"
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.server_label.setText("校准保存失败：" + str(exc))

    def on_annotation_mode_toggled(self, checked: bool) -> None:
        self.annotation_mode = bool(checked)
        if self.annotation_mode:
            self.measure_button.setChecked(False)
            if self.playing:
                self.playing = False
                self.play_timer.stop()
                self.play_button.setText("▶ 开始回放")
            self.annotation_mode_button.setText("结束框选模式")
            self.annotation_status_label.setText(
                "框选模式已开启：在下方漏磁图上按住鼠标左键，拖出一个异常区间。"
            )
        else:
            self.annotation_mode_button.setText("开始框选漏磁区间")
            if self.annotation_drag_start_x is None:
                self.annotation_status_label.setText(
                    "可继续回放；支持五种异常类型。"
                )
        self.mfl_plot.update()

    def _annotation_shortcut_ready(self) -> bool:
        """Return whether a keyboard annotation action has a target range."""
        if self.manifest is None or self.annotation_range() is None:
            return False
        # A loaded annotation can be edited even when the drawing toggle is
        # currently off.  New keyboard labels/saves require the drawing mode.
        return self.annotation_mode or bool(self.annotation_edit_id)

    def select_annotation_label_shortcut(self, code: str, number: int) -> None:
        """Select one annotation type from its numeric keyboard shortcut."""
        if not self._annotation_shortcut_ready():
            return
        index = self.annotation_label_combo.findData(code)
        if index < 0:
            return
        self.annotation_label_combo.setCurrentIndex(index)
        name = ANNOTATION_LABELS.get(code, code)
        action = "更新" if self.annotation_edit_id else "保存"
        self.annotation_status_label.setText(
            f"已选择 {number} · {name}；按 Enter {action}异常标注。"
        )

    def save_annotation_shortcut(self) -> None:
        """Save the current range when Enter is pressed in annotation mode."""
        if not self._annotation_shortcut_ready():
            return
        self.save_annotation()

    def update_annotation_edit_ui(self) -> None:
        editing = bool(self.annotation_edit_id)
        self.annotation_save_button.setText(
            "更新异常标注" if editing else "保存异常标注"
        )
        self.annotation_clear_button.setText(
            "取消编辑" if editing else "清除选区"
        )
        self.annotation_clear_button.setEnabled(
            editing or self.annotation_range() is not None
        )
        self.annotation_reselect_button.setEnabled(
            editing and self.manifest is not None
        )

    def annotation_at_x(self, x_value: float) -> Optional[dict[str, Any]]:
        if not self.annotation_layer_visible:
            return None
        """Return the narrowest saved annotation under the current x position."""
        if not self.annotation_groups or not self.manifest:
            return None
        plot_width = max(1.0, float(self.mfl_plot.geometry_info()["plot_width"]))
        tolerance = max(
            ANNOTATION_MIN_WIDTH_M,
            self.window_width / plot_width * 4.0,
        )
        candidates: list[tuple[float, dict[str, Any]]] = []
        for group in self.annotation_groups:
            group_range = self.annotation_group_range(group)
            if group_range is None:
                continue
            start, end = group_range
            if start - tolerance <= float(x_value) <= end + tolerance:
                candidates.append((max(0.0, end - start), group))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])[1]

    def load_annotation_for_edit(
        self,
        group: dict[str, Any],
        *,
        center: bool = False,
    ) -> None:
        group_range = self.annotation_group_range(group)
        annotation_id = str(group.get("annotation_id", ""))
        if group_range is None or not annotation_id:
            return
        self.annotation_edit_id = annotation_id
        self.annotation_reselecting = False
        self.annotation_drag_start_x = None
        self.annotation_drag_current_x = None
        self.annotation_selection_start_m, self.annotation_selection_end_m = (
            group_range
        )
        for combo, key in (
            (self.annotation_label_combo, "label"),
            (self.annotation_scope_combo, "rail_scope"),
            (self.annotation_direction_combo, "direction_scope"),
        ):
            value = str(group.get(key, ""))
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)
        row = next(
            (
                index
                for index, item in enumerate(self.annotation_groups)
                if str(item.get("annotation_id", "")) == annotation_id
            ),
            -1,
        )
        if row >= 0 and self.annotation_list.currentRow() != row:
            self.annotation_list.blockSignals(True)
            self.annotation_list.setCurrentRow(row)
            self.annotation_list.blockSignals(False)
        if center:
            self.center_on(sum(group_range) / 2.0)
        self.update_annotation_edit_ui()
        self.update_annotation_selection_label()
        self.annotation_status_label.setText(
            f"已加载 {annotation_id}：可以修改异常类型；"
            "如需修改范围，请点击“重新框选范围”。完成后点击“更新异常标注”。"
        )
        self.mfl_plot.update()

    def begin_annotation_reselection(self) -> None:
        if not self.annotation_edit_id:
            return
        self.annotation_reselecting = True
        self.annotation_drag_start_x = None
        self.annotation_drag_current_x = None
        self.annotation_selection_start_m = None
        self.annotation_selection_end_m = None
        if not self.annotation_mode:
            self.annotation_mode_button.setChecked(True)
        self.annotation_clear_button.setEnabled(True)
        self.annotation_status_label.setText(
            f"正在修改 {self.annotation_edit_id}：请在漏磁图上重新拖出范围，"
            "然后点击“更新异常标注”。"
        )
        self.update_annotation_selection_label()
        self.mfl_plot.update()

    def annotation_range(self) -> Optional[tuple[float, float]]:
        if (
            self.annotation_drag_start_x is not None
            and self.annotation_drag_current_x is not None
        ):
            return (
                min(self.annotation_drag_start_x, self.annotation_drag_current_x),
                max(self.annotation_drag_start_x, self.annotation_drag_current_x),
            )
        if (
            self.annotation_selection_start_m is not None
            and self.annotation_selection_end_m is not None
        ):
            return (
                min(self.annotation_selection_start_m, self.annotation_selection_end_m),
                max(self.annotation_selection_start_m, self.annotation_selection_end_m),
            )
        return None

    @staticmethod
    def format_annotation_coordinate(value: float) -> str:
        return f"{float(value):+.3f} m"

    def update_annotation_selection_label(self) -> None:
        selected = self.annotation_range()
        if selected is None:
            self.annotation_selection_label.setText("选中区间：未选择")
            return
        start, end = selected
        self.annotation_selection_label.setText(
            "选中区间："
            + self.format_annotation_coordinate(start)
            + "  →  "
            + self.format_annotation_coordinate(end)
            + f"（宽度 {max(0.0, end - start):.3f} m）"
        )

    def begin_annotation_drag(self, x_value: float) -> None:
        if not self.manifest or not self.annotation_mode:
            return
        if not self.annotation_reselecting:
            self.annotation_edit_id = None
        self.annotation_reselecting = False
        self.annotation_drag_start_x = clamp(
            float(x_value), self.data_start, self.data_end
        )
        self.annotation_drag_current_x = self.annotation_drag_start_x
        self.annotation_selection_start_m = None
        self.annotation_selection_end_m = None
        self.annotation_clear_button.setEnabled(True)
        self.update_annotation_edit_ui()
        self.annotation_status_label.setText(
            "正在框选：松开鼠标后可选择异常类型并保存。"
        )
        self.update_annotation_selection_label()
        self.mfl_plot.update()

    def update_annotation_drag(self, x_value: float) -> None:
        if self.annotation_drag_start_x is None:
            return
        self.annotation_drag_current_x = clamp(
            float(x_value), self.data_start, self.data_end
        )
        self.update_annotation_selection_label()
        self.mfl_plot.update()

    def finish_annotation_drag(self, x_value: float) -> None:
        if self.annotation_drag_start_x is None:
            return
        start = self.annotation_drag_start_x
        end = clamp(float(x_value), self.data_start, self.data_end)
        low, high = min(start, end), max(start, end)
        if high - low < ANNOTATION_MIN_WIDTH_M:
            center = (low + high) / 2.0
            half_width = max(0.01, self.window_width / 400.0)
            low = max(self.data_start, center - half_width)
            high = min(self.data_end, center + half_width)
            if high - low < ANNOTATION_MIN_WIDTH_M:
                self.annotation_status_label.setText(
                    "选区太窄，请拖出更明显的异常区间。"
                )
                self.annotation_drag_start_x = None
                self.annotation_drag_current_x = None
                self.update_annotation_selection_label()
                self.mfl_plot.update()
                return
        self.annotation_drag_start_x = None
        self.annotation_drag_current_x = None
        self.annotation_reselecting = False
        self.annotation_selection_start_m = low
        self.annotation_selection_end_m = high
        self.annotation_clear_button.setEnabled(True)
        self.update_annotation_edit_ui()
        self.annotation_status_label.setText(
            "区间已选中：请选择异常类型后点击“保存异常标注”；"
            "若只是完成查看，请登记为已检查。"
        )
        self.update_annotation_selection_label()
        self.mfl_plot.update()

    def clear_annotation_selection(self) -> None:
        self.annotation_drag_start_x = None
        self.annotation_drag_current_x = None
        self.annotation_selection_start_m = None
        self.annotation_selection_end_m = None
        self.annotation_edit_id = None
        self.annotation_reselecting = False
        self.update_annotation_edit_ui()
        self.annotation_status_label.setText(
            "选区已清除；未登记已检查的区间不自动视为正常。"
        )
        self.update_annotation_selection_label()
        self.mfl_plot.update()

    def selected_annotation_range(
        self,
        *,
        use_view_if_missing: bool = False,
    ) -> Optional[tuple[float, float]]:
        selected = self.annotation_range()
        if selected is None and use_view_if_missing:
            return self.view_range()
        return selected

    def resolve_annotation_spans(
        self,
        start_m: float,
        end_m: float,
    ) -> list[dict[str, Any]]:
        if self.worker.service is None:
            raise RuntimeError("后台数据尚未加载完成")
        return self.worker.service.locate_mfl_range(start_m, end_m)

    def save_annotation(self) -> None:
        selected = self.selected_annotation_range()
        if selected is None:
            self.annotation_status_label.setText(
                "请先在漏磁图上框选一个异常区间。"
            )
            return
        start_m, end_m = selected
        try:
            spans = self.resolve_annotation_spans(start_m, end_m)
            if not spans:
                raise ValueError("当前区间没有对应的漏磁原始记录")
            label = str(self.annotation_label_combo.currentData())
            editing_id = self.annotation_edit_id
            annotation_id = self.annotation_store.save_event(
                spans,
                selected_start_m=start_m,
                selected_end_m=end_m,
                label=label,
                rail_scope=str(self.annotation_scope_combo.currentData()),
                direction_scope=str(self.annotation_direction_combo.currentData()),
                source_window_start_m=self.view_range()[0],
                source_window_end_m=self.view_range()[1],
                us_alignment_offset_m=self.us_alignment_offset_m,
                annotation_id=editing_id,
            )
            self.annotation_groups = self.annotation_store.load_event_groups()
            self.refresh_annotation_list()
            self.annotation_drag_start_x = None
            self.annotation_drag_current_x = None
            self.annotation_selection_start_m = None
            self.annotation_selection_end_m = None
            self.annotation_edit_id = None
            self.annotation_reselecting = False
            self.update_annotation_edit_ui()
            self.update_annotation_selection_label()
            self.annotation_status_label.setText(
                (
                    f"已更新 {annotation_id}"
                    if editing_id
                    else f"已保存 {annotation_id}"
                )
                + f"：{ANNOTATION_LABELS.get(label, label)}；"
                "原始 DAT 片段索引已写入标注目录。"
            )
            self.mfl_plot.update()
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            self.annotation_status_label.setText("异常标注保存失败：" + str(exc))

    def refresh_annotation_list(self) -> None:
        self.update_marker_sources()
        editing_id = self.annotation_edit_id
        self.annotation_list.blockSignals(True)
        self.annotation_list.clear()
        for group in self.annotation_groups:
            spans = group.get("spans", [])
            segments = ",".join(
                dict.fromkeys(str(row.get("mfl_segment", "")) for row in spans)
            )
            try:
                start = float(group.get("selected_x_start_m", 0.0))
                end = float(group.get("selected_x_end_m", 0.0))
                range_text = f"{start:+.3f}~{end:+.3f} m"
            except (TypeError, ValueError):
                range_text = "坐标缺失"
            self.annotation_list.addItem(
                f"{group.get('annotation_id', '')} · "
                f"{group.get('label_name', group.get('label', ''))} · "
                f"{range_text} · {segments}"
            )
        self.annotation_list.blockSignals(False)
        if editing_id:
            row = next(
                (
                    index
                    for index, group in enumerate(self.annotation_groups)
                    if str(group.get("annotation_id", "")) == editing_id
                ),
                -1,
            )
            if row >= 0:
                self.annotation_list.setCurrentRow(row)
        else:
            self.annotation_list.setCurrentRow(-1)
        self.annotation_delete_button.setEnabled(self.annotation_list.count() > 0)
        self.annotation_jump_index = -1
        self.update_annotation_jump_controls()
        self.update_annotation_edit_ui()
        self.mfl_plot.update()

    @staticmethod
    def annotation_group_range(
        group: dict[str, Any],
    ) -> Optional[tuple[float, float]]:
        try:
            start = float(group.get("selected_x_start_m"))
            end = float(group.get("selected_x_end_m"))
        except (TypeError, ValueError):
            return None
        return min(start, end), max(start, end)

    def annotation_jump_targets(self) -> list[dict[str, Any]]:
        selected_label = str(self.annotation_jump_combo.currentData() or "")
        targets = []
        for group in self.annotation_groups:
            if selected_label and str(group.get("label", "")) != selected_label:
                continue
            if self.annotation_group_range(group) is None:
                continue
            targets.append(group)
        return sorted(
            targets,
            key=lambda group: (
                self.annotation_group_range(group)[0],
                str(group.get("annotation_id", "")),
            ),
        )

    def update_annotation_jump_controls(self) -> None:
        targets = self.annotation_jump_targets()
        count = len(targets)
        enabled = bool(targets) and self.manifest is not None
        self.annotation_jump_previous_button.setEnabled(enabled)
        self.annotation_jump_next_button.setEnabled(enabled)
        if not targets:
            self.annotation_jump_status_label.setText("共 0 处")
        elif 0 <= self.annotation_jump_index < count:
            self.annotation_jump_status_label.setText(
                f"{self.annotation_jump_index + 1} / {count}"
            )
        else:
            self.annotation_jump_status_label.setText(f"共 {count} 处")

    def on_annotation_jump_filter_changed(self, _index: int) -> None:
        self.annotation_jump_index = -1
        self.update_annotation_jump_controls()

    def annotation_index_in_view(
        self,
        targets: list[dict[str, Any]],
    ) -> Optional[int]:
        view_start, view_end = self.view_range()
        view_center = (view_start + view_end) / 2.0
        overlapping: list[tuple[float, int]] = []
        for index, group in enumerate(targets):
            group_range = self.annotation_group_range(group)
            if group_range is None:
                continue
            start, end = group_range
            if end >= view_start and start <= view_end:
                overlapping.append(
                    (abs((start + end) / 2.0 - view_center), index)
                )
        if not overlapping:
            return None
        return min(overlapping)[1]

    def jump_to_annotation(self, direction: int) -> None:
        if not self.manifest:
            return
        targets = self.annotation_jump_targets()
        if not targets:
            self.annotation_jump_status_label.setText("共 0 处")
            self.annotation_status_label.setText("当前筛选条件下没有可跳转的标注。")
            return

        current_index = self.annotation_index_in_view(targets)
        if current_index is not None:
            target_index = (current_index + (1 if direction > 0 else -1)) % len(targets)
        else:
            view_start, view_end = self.view_range()
            if direction > 0:
                candidates = [
                    index
                    for index, group in enumerate(targets)
                    if self.annotation_group_range(group)[0] > view_end
                ]
                target_index = candidates[0] if candidates else 0
            else:
                candidates = [
                    index
                    for index, group in enumerate(targets)
                    if self.annotation_group_range(group)[1] < view_start
                ]
                target_index = candidates[-1] if candidates else len(targets) - 1

        target = targets[target_index]
        target_range = self.annotation_group_range(target)
        if target_range is None:
            return
        start, end = target_range
        self.annotation_jump_index = target_index
        self.center_on((start + end) / 2.0)

        annotation_id = str(target.get("annotation_id", ""))
        row = next(
            (
                index
                for index, group in enumerate(self.annotation_groups)
                if str(group.get("annotation_id", "")) == annotation_id
            ),
            -1,
        )
        if row >= 0:
            self.annotation_list.blockSignals(True)
            self.annotation_list.setCurrentRow(row)
            self.annotation_list.blockSignals(False)
        self.annotation_edit_id = None
        self.annotation_reselecting = False
        self.annotation_selection_start_m = None
        self.annotation_selection_end_m = None
        self.update_annotation_edit_ui()
        self.update_annotation_jump_controls()
        label_name = str(target.get("label_name", target.get("label", "")))
        self.annotation_status_label.setText(
            f"已跳转 {annotation_id}：{label_name}，{start:+.3f} ~ {end:+.3f} m"
        )

    def jump_to_previous_annotation(self) -> None:
        self.jump_to_annotation(-1)

    def jump_to_next_annotation(self) -> None:
        self.jump_to_annotation(1)

    def on_annotation_row_changed(self, row: int) -> None:
        self.annotation_delete_button.setEnabled(row >= 0)
        if 0 <= row < len(self.annotation_groups):
            self.load_annotation_for_edit(
                self.annotation_groups[row],
                center=True,
            )
        else:
            self.update_annotation_edit_ui()

    def on_annotation_item_clicked(self, item: Any) -> None:
        row = self.annotation_list.row(item)
        if 0 <= row < len(self.annotation_groups):
            self.load_annotation_for_edit(
                self.annotation_groups[row],
                center=False,
            )

    def delete_selected_annotation(self) -> None:
        row_index = self.annotation_list.currentRow()
        if not 0 <= row_index < len(self.annotation_groups):
            return
        group = self.annotation_groups[row_index]
        annotation_id = str(group.get("annotation_id", ""))
        answer = QMessageBox.question(
            self,
            "删除异常标注",
            f"确定删除 {annotation_id} 及其全部区段吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            if not self.annotation_store.delete_event(annotation_id):
                raise ValueError("标注不存在或已经被删除")
            self.annotation_groups = self.annotation_store.load_event_groups()
            self.annotation_edit_id = None
            self.annotation_reselecting = False
            self.annotation_selection_start_m = None
            self.annotation_selection_end_m = None
            self.refresh_annotation_list()
            self.annotation_status_label.setText(
                f"已删除 {annotation_id}；如需修正，可重新框选后保存。"
            )
        except (OSError, ValueError) as exc:
            self.annotation_status_label.setText("标注删除失败：" + str(exc))

    @staticmethod
    def distance_text(distance: float) -> str:
        return f"{abs(distance) * 1000:.1f} mm" if abs(distance) < 1 else f"{abs(distance):.3f} m"

    def toggle_measurement(self, checked: bool) -> None:
        self.measure_mode = checked
        self.measure_button.setText("结束测量" if checked else "测量两点距离")
        if checked:
            self.annotation_mode_button.setChecked(False)
            if self.playing:
                self.playing = False
                self.play_timer.stop()
                self.play_button.setText("▶ 开始回放")
            self.clear_measurement()

    def clear_measurement(self) -> None:
        self.measure_points = []
        self.measure_label.setText("在超声或漏磁图中依次点击两个信号；再次点击开始新测量。\n距离使用共享标定横轴，不使用超声原始里程。")
        self.us_plot.update()
        self.mfl_plot.update()

    def add_measure_point(self, x: float) -> None:
        if not self.manifest:
            return
        if len(self.measure_points) == 2:
            self.measure_points = []
        self.measure_points.append(clamp(x, self.data_start, self.data_end))
        if len(self.measure_points) == 2:
            a, b = self.measure_points
            self.measure_label.setText(f"A：{a:.4f} m\nB：{b:.4f} m\n沿线路距离：{self.distance_text(b - a)}")
        else:
            self.measure_label.setText(f"A：{self.measure_points[0]:.4f} m\n请选择第二个信号。")
        self.us_plot.update()
        self.mfl_plot.update()

    def draw_dimension(self, painter: QPainter, geometry: dict[str, float],
                       start: float, end: float, y: float, color: QColor,
                       prefix: str = "") -> None:
        low, high = sorted((start, end))
        view_low, view_high = self.view_range()
        if high < view_low or low > view_high:
            return
        left = geometry["left"]
        right = left + geometry["plot_width"]
        x1 = self.mfl_plot.x_to_pixel(low, geometry)
        x2 = self.mfl_plot.x_to_pixel(high, geometry)
        a, b = max(left, x1), min(right, x2)
        painter.save()
        painter.setPen(QPen(color, 1.0))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawLine(QPointF(a, y), QPointF(b, y))
        for x, direction in ((x1, 1), (x2, -1)):
            if left <= x <= right:
                extension_top = geometry["top"] + geometry["plot_height"] + 25 if prefix == "宽 " else y - 7
                painter.drawLine(QPointF(x, extension_top), QPointF(x, y + 5))
                painter.drawLine(QPointF(x, y), QPointF(x + direction * 5, y - 3))
                painter.drawLine(QPointF(x, y), QPointF(x + direction * 5, y + 3))
        text = prefix + self.distance_text(high - low)
        if low < view_low or high > view_high:
            text += "（全宽）"
        width = painter.fontMetrics().horizontalAdvance(text) + 10
        center = clamp((a + b) / 2, left + width / 2, right - width / 2)
        painter.drawText(QRectF(center - width / 2, y + 3, width, 17), Qt.AlignCenter, text)
        painter.restore()

    def draw_measurement(self, painter: QPainter, geometry: dict[str, float]) -> None:
        color = QColor("#E08B00")
        painter.save()
        painter.setPen(QPen(color, 1.3, Qt.DashLine))
        low, high = self.view_range()
        for index, x in enumerate(self.measure_points):
            if low <= x <= high:
                pixel = self.mfl_plot.x_to_pixel(x, geometry)
                painter.drawLine(QPointF(pixel, geometry["top"]), QPointF(pixel, geometry["top"] + geometry["plot_height"]))
                painter.drawText(QPointF(pixel + 4, geometry["top"] + 28), "AB"[index])
        painter.restore()
        if len(self.measure_points) == 2:
            self.draw_dimension(painter, geometry, *self.measure_points,
                                geometry["top"] + geometry["plot_height"] + geometry["bottom"] - 27, color, "距离 ")

    def draw_annotation_dimensions(self, painter: QPainter, geometry: dict[str, float]) -> None:
        if not self.annotation_layer_visible:
            return
        ranges = []
        for group in self.annotation_groups:
            if str(group.get("annotation_id", "")) == self.annotation_edit_id:
                continue
            span = self.annotation_group_range(group)
            if span:
                ranges.append((*span, QColor(ANNOTATION_COLORS.get(str(group.get("label")), "#21B8C8"))))
        selection = self.annotation_range()
        if selection:
            ranges.append((*selection, QColor("#E08B00")))
        for start, end, color in ranges:
            self.draw_dimension(painter, geometry, start, end,
                                geometry["top"] + geometry["plot_height"] + 36, color, "宽 ")

    def draw_model_predictions(
        self,
        painter: QPainter,
        geometry: dict[str, float],
        plot_kind: str,
    ) -> None:
        """Draw model intervals on both synchronized plots.

        Model output is deliberately a separate visual layer.  It is useful
        for review, but it must not look like a saved human truth label.
        """
        targets = self.model_result_targets()
        if not targets:
            return
        view_start, view_end = self.view_range()
        visible = [
            item
            for item in targets
            if float(item.get("x_end_m", 0.0)) >= view_start
            and float(item.get("x_start_m", 0.0)) <= view_end
        ]
        if not visible:
            return

        plot_left = geometry["left"]
        plot_right = plot_left + geometry["plot_width"]
        plot_top = geometry["top"]
        plot_bottom = plot_top + geometry["plot_height"]
        block_gap = min(24.0, max(16.0, geometry["plot_height"] * 0.06))
        block_height = max(1.0, (geometry["plot_height"] - block_gap) / 2.0)

        painter.save()
        painter.setClipRect(
            QRectF(plot_left, plot_top, geometry["plot_width"], geometry["plot_height"])
        )
        painter.setFont(QFont("Segoe UI", 8))
        for item in visible:
            try:
                start = float(item.get("x_start_m", 0.0))
                end = float(item.get("x_end_m", 0.0))
            except (TypeError, ValueError):
                continue
            x1 = self.mfl_plot.x_to_pixel(min(start, end), geometry)
            x2 = self.mfl_plot.x_to_pixel(max(start, end), geometry)
            left = max(plot_left, min(x1, x2))
            right = min(plot_right, max(x1, x2))
            if right < plot_left or left > plot_right:
                continue
            width = max(1.0, right - left)

            if plot_kind == "mfl":
                scope = str(item.get("scope", "all"))
                if scope == "left":
                    top, bottom = plot_top, plot_top + block_height
                elif scope == "right":
                    top = plot_top + block_height + block_gap
                    bottom = top + block_height
                else:
                    top, bottom = plot_top, plot_bottom
            else:
                top, bottom = plot_top, plot_bottom

            color = QColor(str(item.get("color", "#FF66B3")))
            selected = (
                str(item.get("prediction_id", ""))
                == str(self.model_result_selected_id)
            )
            fill = QColor(color)
            fill.setAlpha(48 if selected else 20)
            painter.setBrush(fill)
            painter.setPen(
                QPen(
                    color,
                    2.2 if selected else 1.0,
                    Qt.SolidLine if selected else Qt.DashLine,
                )
            )
            painter.drawRect(QRectF(left, top, width, max(1.0, bottom - top)))

            if selected or (len(visible) <= 10 and width >= 42.0):
                label = str(item.get("label_name", "模型异常候选"))
                label_width = max(46.0, float(painter.fontMetrics().horizontalAdvance(label) + 6))
                painter.setPen(color)
                painter.drawText(
                    QRectF(left + 2.0, top + 2.0, label_width, 15.0),
                    Qt.AlignLeft | Qt.AlignTop,
                    label,
                )
        painter.restore()

    def draw_saved_annotations(
        self,
        painter: QPainter,
        geometry: dict[str, float],
    ) -> None:
        if not self.annotation_layer_visible or not self.annotation_groups:
            return
        plot_rect = QRectF(
            geometry["left"],
            geometry["top"],
            geometry["plot_width"],
            geometry["plot_height"],
        )
        painter.save()
        painter.setClipRect(plot_rect)
        for group in self.annotation_groups:
            try:
                start = float(group.get("selected_x_start_m"))
                end = float(group.get("selected_x_end_m"))
            except (TypeError, ValueError):
                continue
            x_start = self.mfl_plot.x_to_pixel(min(start, end), geometry)
            x_end = self.mfl_plot.x_to_pixel(max(start, end), geometry)
            left = min(x_start, x_end)
            width = max(1.0, abs(x_end - x_start))
            code = str(group.get("label", ""))
            color = QColor(ANNOTATION_COLORS.get(code, "#FFFFFF"))
            fill = QColor(color)
            selected = (
                str(group.get("annotation_id", "")) == self.annotation_edit_id
            )
            fill.setAlpha(62 if selected else 34)
            painter.setPen(QPen(color, 2.2 if selected else 1.0, Qt.SolidLine))
            painter.setBrush(fill)
            painter.drawRect(
                QRectF(left, geometry["top"], width, geometry["plot_height"])
            )
            if width >= 42.0:
                painter.setPen(color)
                painter.drawText(
                    QRectF(left + 3.0, geometry["top"] + 2.0, width - 6.0, 15.0),
                    Qt.AlignLeft | Qt.AlignTop,
                    str(group.get("label_name", code)),
                )
        painter.restore()

    def draw_annotation_selection(
        self,
        painter: QPainter,
        geometry: dict[str, float],
    ) -> None:
        if not self.annotation_layer_visible and self.annotation_edit_id:
            return
        selected = self.annotation_range()
        if selected is None:
            return
        start, end = selected
        x_start = self.mfl_plot.x_to_pixel(start, geometry)
        x_end = self.mfl_plot.x_to_pixel(end, geometry)
        left = min(x_start, x_end)
        width = max(1.0, abs(x_end - x_start))
        painter.save()
        painter.setClipRect(
            QRectF(
                geometry["left"],
                geometry["top"],
                geometry["plot_width"],
                geometry["plot_height"],
            )
        )
        color = QColor("#FFE45C")
        fill = QColor(color)
        fill.setAlpha(35)
        painter.setBrush(fill)
        painter.setPen(QPen(color, 1.0, Qt.DashLine))
        painter.drawRect(
            QRectF(left, geometry["top"], width, geometry["plot_height"])
        )
        painter.setPen(QPen(color, 1.2, Qt.SolidLine))
        painter.drawLine(
            QPointF(x_start, geometry["top"]),
            QPointF(x_start, self.mfl_plot.height() - geometry["bottom"]),
        )
        painter.drawLine(
            QPointF(x_end, geometry["top"]),
            QPointF(x_end, self.mfl_plot.height() - geometry["bottom"]),
        )
        painter.restore()

    def show_gate_legend(self) -> None:
        if not self.manifest:
            return
        dialog = GateLegendDialog(self)
        dialog.exec_()

    def on_timeline_changed(self, value: int) -> None:
        if not self.manifest:
            return
        span = max(0.0, self.data_end - self.data_start - self.window_width)
        self.set_position(self.data_start + value / 100000.0 * span, request=False)
        self.schedule_request()

    def set_position(self, value: float, request: bool = True) -> None:
        if not self.manifest:
            return
        previous_start = self.desired_x_start
        self.desired_x_start = clamp(float(value), self.data_start, self.max_start())
        if self.desired_x_start > previous_start:
            self.navigation_direction = 1
        elif self.desired_x_start < previous_start:
            self.navigation_direction = -1
        self.view_x_start = self.desired_x_start
        buffered = self.find_cached_buffer()
        if buffered is not None:
            self.data = buffered
            self.fetch_pending = False
            # Keep the current scale while stepping through a prefetched
            # buffer. Recomputing P99 over every raw point on each key/wheel
            # event causes visible navigation stalls; the scale is refreshed
            # when a new worker payload arrives.
        self.start_spin.blockSignals(True)
        self.start_spin.setValue(self.desired_x_start)
        self.start_spin.blockSignals(False)
        self.update_timeline()
        self.us_plot.update()
        self.mfl_plot.update()
        if request:
            if buffered is not None:
                self.schedule_prefetch()
            else:
                # Queue the missing visible buffer immediately. The worker is
                # asynchronous, so this does not block the key event.
                self.request_timer.stop()
                self.request_window()

    def move_position(self, delta: float) -> None:
        self.set_position(self.desired_x_start + delta)

    def center_on(self, x_value: float) -> None:
        self.set_position(float(x_value) - self.window_width / 2)

    def set_cursor(self, x_value: float) -> None:
        x_start, x_end = self.view_range()
        if x_start <= x_value <= x_end:
            self.cursor_x = float(x_value)
            self.cursor_label.setText("游标：里程见横坐标，X/Z 见漏磁右侧")
            if not self.cursor_timer.isActive():
                self.cursor_timer.start()
            self.us_plot.update()
            self.mfl_plot.update()

    def request_cursor_sample(self) -> None:
        if self.cursor_x is None or not self.worker.isRunning():
            return
        self.cursor_request_serial += 1
        self.worker.request_cursor(self.cursor_request_serial, self.cursor_x)

    def on_cursor_ready(self, serial: int, payload: dict[str, Any]) -> None:
        if serial != self.cursor_request_serial or self.cursor_x is None:
            return
        self.cursor_sample = payload
        self.mfl_plot.update()

    def clear_cursor(self) -> None:
        if self.cursor_x is not None:
            self.cursor_timer.stop()
            self.cursor_request_serial += 1
            self.cursor_x = None
            self.cursor_sample = None
            self.cursor_label.setText("光标：—")
            self.us_plot.update()
            self.mfl_plot.update()

    def update_timeline(self) -> None:
        if not self.manifest:
            return
        span = max(0.000001, self.data_end - self.data_start - self.window_width)
        ratio = clamp((self.desired_x_start - self.data_start) / span, 0.0, 1.0)
        self.timeline.blockSignals(True)
        self.timeline.setValue(int(ratio * 100000))
        self.timeline.blockSignals(False)
        self.range_label.setText(
            "范围：" + format_distance(self.view_x_start)
            + "  →  " + format_distance(self.view_x_start + self.window_width)
            + "    接头：0.00 m"
        )

    def schedule_request(self) -> None:
        if self.manifest:
            self.request_timer.start(80)

    def current_filter_signature(self) -> tuple[Any, ...]:
        gates = self.selected_gates()
        return (
            tuple(self.selected_tracks()),
            tuple(gates) if gates is not None else None,
        )

    @staticmethod
    def payload_filter_signature(payload: dict[str, Any]) -> tuple[Any, ...]:
        filters = payload.get("filters", {})
        tracks = filters.get("tracks")
        gates = filters.get("gates")
        return (
            tuple(sorted(int(value) for value in tracks))
            if tracks is not None
            else None,
            tuple(sorted(int(value) for value in gates))
            if gates is not None
            else None,
        )

    def buffer_margin_m(self) -> float:
        # For the default 8 m viewport this keeps 12 m on each side. A
        # single read therefore feeds many arrow-key steps.
        return min(24.0, max(6.0, self.window_width * 1.5))

    def buffer_span_m(self) -> float:
        return self.window_width + 2.0 * self.buffer_margin_m()

    def planned_buffer_range(self, view_start: float) -> tuple[float, float]:
        margin = self.buffer_margin_m()
        target_span = min(
            max(0.001, self.data_end - self.data_start),
            self.window_width + 2.0 * margin,
        )
        fetch_start = max(self.data_start, float(view_start) - margin)
        fetch_end = min(
            self.data_end,
            float(view_start) + self.window_width + margin,
        )
        if fetch_end - fetch_start < target_span:
            if fetch_start <= self.data_start + 0.0001:
                fetch_end = min(self.data_end, fetch_start + target_span)
            elif fetch_end >= self.data_end - 0.0001:
                fetch_start = max(self.data_start, fetch_end - target_span)
        return fetch_start, fetch_end

    def buffer_key(
        self,
        fetch_start: float,
        fetch_end: float,
        signature: Optional[tuple[Any, ...]] = None,
    ) -> tuple[Any, ...]:
        tracks, gates = signature or self.current_filter_signature()
        return (
            round(float(fetch_start), 3),
            round(float(fetch_end), 3),
            tracks,
            gates,
        )

    def key_matches_current_filters(self, key: tuple[Any, ...]) -> bool:
        return len(key) >= 4 and (key[2], key[3]) == self.current_filter_signature()

    def key_covers_current_view(self, key: tuple[Any, ...]) -> bool:
        if not self.key_matches_current_filters(key):
            return False
        view_end = self.desired_x_start + self.window_width
        return (
            float(key[0]) <= self.desired_x_start + 0.0001
            and float(key[1]) >= view_end - 0.0001
        )

    def payload_covers_current_view(
        self,
        payload: Optional[dict[str, Any]],
    ) -> bool:
        if not payload:
            return False
        if self.payload_filter_signature(payload) != self.current_filter_signature():
            return False
        try:
            fetch_start = float(payload.get("xStartM"))
            fetch_end = float(payload.get("xEndM"))
        except (TypeError, ValueError):
            return False
        view_end = self.desired_x_start + self.window_width
        return (
            fetch_start <= self.desired_x_start + 0.0001
            and fetch_end >= view_end - 0.0001
        )

    def find_cached_buffer(self) -> Optional[dict[str, Any]]:
        if self.payload_covers_current_view(self.data):
            return self.data
        candidates = [
            (key, payload)
            for key, payload in self.window_cache.items()
            if self.key_covers_current_view(key)
        ]
        if not candidates:
            return None
        if self.navigation_direction >= 0:
            key, payload = max(candidates, key=lambda item: float(item[0][1]))
        else:
            key, payload = min(candidates, key=lambda item: float(item[0][0]))
        self.window_cache.move_to_end(key)
        return payload

    def pending_buffer_covers_current_view(self) -> bool:
        return any(
            self.key_covers_current_view(key)
            for key in self.pending_request_keys
        )

    def cache_window(self, key: Optional[tuple[Any, ...]], payload: dict[str, Any]) -> None:
        if key is None:
            return
        self.window_cache[key] = payload
        self.window_cache.move_to_end(key)
        while len(self.window_cache) > 7:
            self.window_cache.popitem(last=False)

    def apply_buffer_payload(
        self,
        payload: dict[str, Any],
        from_cache: bool,
    ) -> None:
        initial_window = self.data is None and self.manifest is not None
        self.data = payload
        self.fetch_pending = False
        self.view_x_start = self.desired_x_start
        if initial_window:
            self.load_progress.setValue(1000)
            self.load_progress.setFormat("已完成 100%")
        self.update_mfl_scale()
        self.update_timeline()
        us = payload.get("ultrasound", {})
        mfl = payload.get("mfl", {})
        self.data_label.setText(
            f"数据集：{self.dataset_name}\n"
            + "US \u7f13\u51b2 " + format_count(us.get("count", 0))
            + ("\uff08\u62bd\u6837\uff09" if us.get("decimated") else "")
            + "\nMFL \u7f13\u51b2 "
            + format_count(mfl.get("rawRecordsPerChannel", 0))
            + " / \u901a\u9053"
        )
        try:
            buffered_ahead = max(
                0.0,
                float(payload.get("xEndM"))
                - (self.desired_x_start + self.window_width),
            )
        except (TypeError, ValueError):
            buffered_ahead = 0.0
        status = "\u7f13\u51b2\u547d\u4e2d" if from_cache else "\u7a97\u53e3\u5df2\u52a0\u8f7d"
        self.server_label.setText(
            "\u72b6\u6001\uff1a" + status
            + " \u00b7 " + format_distance(self.view_x_start)
            + " \u2192 " + format_distance(self.view_x_start + self.window_width)
        )
        self.us_plot.update()
        self.mfl_plot.update()

    def start_buffer_request(
        self,
        fetch_start: float,
        fetch_end: float,
        prefetch: bool,
    ) -> bool:
        key = self.buffer_key(fetch_start, fetch_end)
        if key in self.window_cache or key in self.pending_request_keys:
            return False
        self.request_serial += 1
        serial = self.request_serial
        self.request_key_by_serial[serial] = key
        self.pending_request_keys.add(key)
        if prefetch:
            self.prefetch_serials.add(serial)
        else:
            self.fetch_pending = True
            self.last_request_time = time.monotonic()
            self.server_label.setText("\u6b63\u5728\u8bfb\u53d6\u5f53\u524d\u7f13\u51b2\u533a\u2026")
        self.worker.request_window(
            serial,
            fetch_start,
            fetch_end,
            self.selected_tracks(),
            self.selected_gates(),
            priority=not prefetch,
        )
        return True

    def request_window(self) -> None:
        if not self.manifest or not self.worker.isRunning():
            return
        cached = self.find_cached_buffer()
        if cached is not None:
            self.last_request_time = time.monotonic()
            self.apply_buffer_payload(cached, from_cache=True)
            self.schedule_prefetch()
            return
        if self.pending_buffer_covers_current_view():
            self.fetch_pending = True
            self.server_label.setText("\u6b63\u5728\u7b49\u5f85\u9884\u53d6\u7f13\u51b2\u2026")
            return
        fetch_start, fetch_end = self.planned_buffer_range(self.desired_x_start)
        self.start_buffer_request(fetch_start, fetch_end, prefetch=False)

    def schedule_prefetch(self) -> None:
        if self.manifest and self.worker.isRunning():
            self.prefetch_timer.start(15)

    def prefetch_next_buffer(self) -> None:
        if not self.manifest or not self.worker.isRunning():
            return
        signature = self.current_filter_signature()
        all_keys = set(self.window_cache.keys()) | set(self.pending_request_keys)
        matching = [
            key
            for key in all_keys
            if len(key) >= 4 and (key[2], key[3]) == signature
        ]
        if not matching:
            return

        view_start = self.desired_x_start
        view_end = view_start + self.window_width
        threshold = max(0.5, self.window_width * 0.25)
        span = self.buffer_span_m()
        overlap = min(self.window_width, max(2.0, span * 0.25))

        if self.navigation_direction >= 0:
            useful = [key for key in matching if float(key[1]) > view_end + threshold]
            if len(useful) >= self.buffer_target_count:
                return
            anchor_end = max(float(key[1]) for key in matching)
            if anchor_end >= self.data_end - 0.0001:
                return
            next_start = max(self.data_start, anchor_end - overlap)
            next_end = min(self.data_end, next_start + span)
            if next_end - next_start < min(self.window_width, span):
                next_start = max(self.data_start, next_end - span)
        else:
            useful = [key for key in matching if float(key[0]) < view_start - threshold]
            if len(useful) >= self.buffer_target_count:
                return
            anchor_start = min(float(key[0]) for key in matching)
            if anchor_start <= self.data_start + 0.0001:
                return
            next_end = min(self.data_end, anchor_start + overlap)
            next_start = max(self.data_start, next_end - span)
            if next_end - next_start < min(self.window_width, span):
                next_end = min(self.data_end, next_start + span)

        self.start_buffer_request(next_start, next_end, prefetch=True)

    def on_window_ready(self, serial: int, payload: dict[str, Any]) -> None:
        key = self.request_key_by_serial.pop(serial, None)
        if key is not None:
            self.pending_request_keys.discard(key)
            self.cache_window(key, payload)
        was_prefetch = serial in self.prefetch_serials
        self.prefetch_serials.discard(serial)

        selected = self.find_cached_buffer()
        if selected is not None:
            self.fetch_pending = False
            if selected is not self.data or not was_prefetch:
                self.apply_buffer_payload(
                    selected,
                    from_cache=was_prefetch or selected is not payload,
                )
            self.schedule_prefetch()
            return

        if not self.pending_buffer_covers_current_view():
            self.fetch_pending = False
            self.request_window()

    def update_mfl_scale(self) -> None:
        if not self.data:
            return
        rows = self.data.get("mfl", {}).get("rows", [])
        limits: list[float] = []
        actual_ranges: list[tuple[float, float]] = []
        peak_values: list[float] = []
        clipped_counts: list[int] = []
        view_start, view_end = self.view_range()

        fixed_limit: Optional[float] = None
        if self.mfl_scale_mode != "auto":
            try:
                fixed_limit = max(MFL_Y_LIMIT_MIN, float(self.mfl_scale_mode))
            except (TypeError, ValueError):
                fixed_limit = MFL_Y_LIMIT_DEFAULT

        for row_index in range(16):
            values: list[float] = []
            if row_index < len(rows):
                for segment in rows[row_index].get("segments", []):
                    x_positions = segment.get("x", [])
                    visible_start, visible_stop = visible_index_range(
                        x_positions,
                        view_start,
                        view_end,
                    )
                    for key in ("xValue", "zValue"):
                        signal_values = segment.get(key, [])
                        stop = min(visible_stop, len(x_positions), len(signal_values))
                        block = np.asarray(signal_values[visible_start:stop], dtype=np.float64)
                        if np.any(np.isfinite(block)):
                            values.append(block[np.isfinite(block)])

            if values:
                array = np.concatenate(values)
                lower = float(array.min())
                upper = float(array.max())
                peak = max(abs(lower), abs(upper))
                absolute_values = np.abs(array)
                p99_index = min(len(array) - 1, int(round(0.99 * (len(array) - 1))))
                robust_level = float(np.partition(absolute_values, p99_index)[p99_index])
                auto_limit = max(MFL_Y_LIMIT_MIN, robust_level * 1.35)
                limit = fixed_limit if fixed_limit is not None else auto_limit
                clipped = int(np.count_nonzero(absolute_values > limit))
            else:
                lower = 0.0
                upper = 0.0
                peak = 0.0
                limit = fixed_limit if fixed_limit is not None else MFL_Y_LIMIT_DEFAULT
                clipped = 0

            limits.append(max(MFL_Y_LIMIT_MIN, float(limit)))
            actual_ranges.append((lower, upper))
            peak_values.append(peak)
            clipped_counts.append(clipped)

        self.mfl_y_limits = limits
        self.mfl_actual_ranges = actual_ranges
        self.mfl_peak_values = peak_values
        self.mfl_clipped_counts = clipped_counts

        if self.mfl_scale_mode == "auto":
            clipped_total = sum(clipped_counts)
            self.mfl_scale_label.setText(
                "MFL\uff1a\u9010\u884c\u81ea\u52a8\uff08P99 \u00d7 1.35\uff09\n"
                + f"\u5355\u4f4d DAT.value / 1000\uff1b\u8d85\u91cf\u7a0b {format_count(clipped_total)} \u70b9"
            )
        else:
            self.mfl_scale_label.setText(
                f"MFL\uff1a\u7edf\u4e00 \u00b1{format_signal_value(limits[0])}\n"
                "\u53f3\u4fa7\u968f\u6e38\u6807\u663e\u793a\u5404\u884c X / Z \u503c"
            )

    def update_timeline_after_play(self) -> None:
        self.view_x_start = self.desired_x_start
        buffered = self.find_cached_buffer()
        if buffered is not None:
            self.data = buffered
            self.fetch_pending = False
            self.schedule_prefetch()
        self.update_timeline()
        self.us_plot.update()
        self.mfl_plot.update()

    def toggle_play(self) -> None:
        self.playing = not self.playing
        self.play_button.setText("Ⅱ 暂停回放" if self.playing else "▶ 开始回放")
        if self.playing:
            self.last_play_tick = time.monotonic()
            self.play_timer.start()
        else:
            self.play_timer.stop()

    def play_tick(self) -> None:
        if not self.playing:
            return
        now = time.monotonic()
        elapsed = min(0.2, max(0.0, now - self.last_play_tick))
        self.last_play_tick = now
        next_start = self.desired_x_start + self.speed * elapsed
        if next_start >= self.max_start():
            self.set_position(self.max_start())
            self.playing = False
            self.play_timer.stop()
            self.play_button.setText("▶ 开始回放")
            return
        self.desired_x_start = clamp(next_start, self.data_start, self.max_start())
        self.update_timeline_after_play()
        if not self.payload_covers_current_view(self.data) and not self.fetch_pending:
            self.request_window()
        elif now - self.last_request_time > 0.45:
            self.last_request_time = now
            self.schedule_prefetch()

    def select_all_gates(self) -> None:
        for button in self.gate_buttons.values():
            button.setChecked(True)
        self.schedule_request()

    def clear_all_gates(self) -> None:
        for button in self.gate_buttons.values():
            button.setChecked(False)
        self.schedule_request()

    def draw_x_grid(
        self,
        painter: QPainter,
        geometry: dict[str, float],
        draw_labels: bool,
    ) -> None:
        x_start, x_end = self.view_range()
        painter.save()
        painter.setFont(QFont("Segoe UI", 8))
        label_width = max(painter.fontMetrics().horizontalAdvance(format_distance(v)) for v in (x_start, x_end)) + 20
        tick_count = max(1, min(6, int(geometry["plot_width"] / max(90, label_width))))
        for index in range(tick_count + 1):
            ratio = index / tick_count
            x = geometry["left"] + ratio * geometry["plot_width"]
            pen_color = QColor("#334956") if index in (0, tick_count) else QColor("#1b303e")
            painter.setPen(QPen(pen_color, 1))
            # Keep the shared x-grid as a short top-edge tick. Drawing it
            # through all 16 MFL rows makes every channel look over-gridded.
            painter.drawLine(
                QPointF(x, geometry["top"] + 1.0),
                QPointF(x, geometry["top"] + 9.0),
            )
            if draw_labels:
                value = x_start + ratio * (x_end - x_start)
                painter.setPen(QColor("#78909e"))
                painter.drawText(
                    QRectF(x - 60, geometry["top"] + geometry["plot_height"] + 7, 120, 18),
                    Qt.AlignCenter | Qt.AlignTop,
                    format_distance(value),
                )
        painter.restore()

    def draw_cursor(self, painter: QPainter, geometry: dict[str, float]) -> None:
        if self.cursor_x is None:
            return
        x_start, x_end = self.view_range()
        if not x_start <= self.cursor_x <= x_end:
            return
        x = geometry["left"] + (
            (self.cursor_x - x_start) / (x_end - x_start) * geometry["plot_width"]
        )
        painter.save()
        cursor_color = QColor(CURSOR_COLOR)
        painter.setPen(QPen(cursor_color, 1, Qt.SolidLine))
        painter.drawLine(
            QPointF(x, geometry["top"]),
            QPointF(x, geometry["top"] + geometry["plot_height"]),
        )
        label_width = 112.0
        label_x = clamp(
            x - label_width / 2.0,
            geometry["left"],
            geometry["left"] + geometry["plot_width"] - label_width,
        )
        axis_y = geometry["top"] + geometry["plot_height"]
        label_y = axis_y + 3.0
        painter.setPen(QPen(QColor("#FF817A"), 1.0))
        painter.setBrush(cursor_color)
        painter.drawRoundedRect(QRectF(label_x, label_y, label_width, 21), 3, 3)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(
            QRectF(label_x + 2, label_y + 1, label_width - 4, 19),
            Qt.AlignCenter,
            "里程 " + format_distance(self.cursor_x),
        )
        painter.restore()

    def draw_segment_markers(
        self,
        painter: QPainter,
        geometry: dict[str, float],
        rows: list[dict[str, Any]],
    ) -> None:
        if not rows:
            return
        segments = rows[0].get("segments", [])
        if len(segments) < 2:
            return
        theme = MFL_THEMES.get(self.mfl_background_mode, MFL_THEMES["dark"])
        segment_color = QColor(theme["segment"])
        painter.save()
        painter.setFont(QFont("Segoe UI", 8))
        for segment in segments:
            xs = segment.get("x", [])
            if not xs:
                continue
            x = self.mfl_plot.x_to_pixel(float(xs[0]), geometry)
            if geometry["left"] <= x <= geometry["left"] + geometry["plot_width"]:
                painter.setPen(QPen(segment_color, 1, Qt.DashLine))
                painter.drawLine(
                    QPointF(x, geometry["top"]),
                    QPointF(x, self.mfl_plot.height() - geometry["bottom"]),
                )
                painter.setPen(segment_color)
                painter.drawText(
                    QRectF(x + 4, geometry["top"], 80, 16),
                    Qt.AlignLeft | Qt.AlignTop,
                    str(segment.get("name", "")),
                )
        painter.restore()

    def _joint_cache_context(self) -> tuple[Path, str]:
        """Return the cache file and source fingerprint for the current MFL data."""
        config = _read_json_config(self.config_path)
        raw_mfl_path = config.get("mfl_data_dir")
        if raw_mfl_path:
            mfl_path = Path(str(raw_mfl_path))
            if not mfl_path.is_absolute():
                mfl_path = self.config_path.parent / mfl_path
        else:
            mfl_path = (
                self.dataset_root
                / "data"
                / self.dataset_name
                / "漏磁"
            )
        mfl_path = mfl_path.resolve()

        source_files: list[tuple[str, int, int]] = []
        try:
            for path in sorted(mfl_path.rglob("*.dat")):
                stat = path.stat()
                source_files.append(
                    (
                        path.relative_to(mfl_path).as_posix(),
                        int(stat.st_size),
                        int(stat.st_mtime_ns),
                    )
                )
        except OSError as exc:
            error_digest = hashlib.sha256(str(exc).encode("utf-8")).hexdigest()
            source_files.append(("<stat-error>", 0, int(error_digest[:16], 16)))

        source_payload = {
            "algorithm": "absolute_max_v1",
            "dataset": self.dataset_name,
            "mflPath": str(mfl_path),
            "files": source_files,
            "scaleMmPerUnit": config.get("scale_mm_per_unit"),
            "directionSign": config.get("direction_sign"),
            "joint": {
                key: config.get("joint", {}).get(key)
                for key in ("mfl_segment", "mfl_record_pos", "mfl_raw_index")
            },
        }
        source_signature = hashlib.sha256(
            json.dumps(
                source_payload,
                ensure_ascii=True,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        cache_id = hashlib.sha256(
            f"{self.dataset_name}|{mfl_path}".encode("utf-8")
        ).hexdigest()[:24]

        cache_root = APP_DIR / "cache" / "joint_location"
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            cache_root = (
                Path(tempfile.gettempdir())
                / "ultrasound_mfl_replay_cache"
                / "joint_location"
            )
            try:
                cache_root.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        return cache_root / f"{cache_id}.json", source_signature

    def _load_joint_cache(self) -> Optional[dict[str, Any]]:
        try:
            cache_path, source_signature = self._joint_cache_context()
            if not cache_path.is_file():
                return None
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if payload.get("sourceSignature") != source_signature:
                return None
            result = payload.get("result")
            if not isinstance(result, dict):
                return None
            cached_result = dict(result)
            cached_result["_fromCache"] = True
            ultrasound_available = bool(
                self.manifest
                and self.manifest.get("ultrasound", {}).get("available")
            )
            cached_result["ultrasound"] = {
                "available": ultrasound_available,
                "confidence": "not_used",
                "usedForSelection": False,
            }
            return cached_result
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _save_joint_cache(self, result: dict[str, Any]) -> None:
        try:
            cache_path, source_signature = self._joint_cache_context()
            cache_result = {
                key: value
                for key, value in result.items()
                if not key.startswith("_")
            }
            temporary_path = cache_path.with_name(
                f".{cache_path.name}.{os.getpid()}.tmp"
            )
            temporary_path.write_text(
                json.dumps(
                    {
                        "cacheVersion": "absolute_max_v1",
                        "sourceSignature": source_signature,
                        "result": cache_result,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(cache_path)
        except (OSError, TypeError, ValueError):
            # A read-only installation must not make locating a joint fail.
            return

    def _set_operation_status(self, message: str) -> None:
        """Show one concise operation line; detailed progress remains in the bar."""
        text = " ".join(str(message).replace("\r", " ").replace("\n", " ").split())
        text = re.sub(
            r"^(?:加载进度|接头定位)\s*\d+(?:\.\d+)?%\s*[：:]\s*",
            "",
            text,
        )
        if len(text) > 58:
            text = text[:55].rstrip() + "…"
        self.server_label.setText(f"状态：{text or '空闲'}")

    def _set_joint_search_buttons_enabled(self, enabled: bool) -> None:
        has_data = self.manifest is not None
        ultrasound_available = bool(
            has_data
            and self.manifest.get("ultrasound", {}).get("available", False)
        )
        self.joint_button.setEnabled(bool(enabled and has_data))
        self.ultrasound_joint_button.setEnabled(
            bool(enabled and ultrasound_available)
        )

    def locate_joint(self) -> None:
        if (
            self.manifest is None
            or not self.worker.isRunning()
            or self._stopping_worker is not None
        ):
            return

        cached_result = self._load_joint_cache()
        if cached_result is not None:
            self.on_joint_ready(cached_result)
            return

        self._set_joint_search_buttons_enabled(False)
        self.joint_detection_status = ""
        self._set_operation_status("正在扫描漏磁绝对值最大点…")
        self.health_label.setText("接头定位扫描中…")
        self.worker.request_joint_search()

    def locate_ultrasound_joint(self) -> None:
        if (
            self.manifest is None
            or not self.manifest.get("ultrasound", {}).get("available", False)
            or not self.worker.isRunning()
            or self._stopping_worker is not None
        ):
            return

        self._set_joint_search_buttons_enabled(False)
        self._set_operation_status("正在扫描超声多Gate双Track接头特征…")
        self.health_label.setText("超声接头检测扫描中…")
        self.worker.request_ultrasound_joint_search()

    def on_joint_progress(self, message: str) -> None:
        self.health_label.setText(message)
        self._set_operation_status(message)

    def on_joint_ready(self, result: dict[str, Any]) -> None:
        self._set_joint_search_buttons_enabled(True)
        from_cache = bool(result.pop("_fromCache", False))
        try:
            x_m = float(result["xM"])
            segment = str(result.get("segment", "?"))
            record_pos = int(result.get("recordPos", 0))
        except (KeyError, TypeError, ValueError) as exc:
            self.on_joint_failed(f"接头定位结果无效：{exc}")
            return

        if not from_cache:
            self._save_joint_cache(result)

        confidence = str(result.get("confidence", "low"))
        confidence_text = {
            "high": "高置信度",
            "medium": "中等置信度",
            "low": "低置信度，请人工确认",
            "direct": "直接使用绝对值最大值",
        }.get(confidence, confidence)
        method_text = (
            "全数据绝对值最大值"
            if result.get("method") == "absolute_max"
            else "多通道边界响应"
            if result.get("method") == "boundary_consensus"
            else "多通道鲁棒幅值"
        )
        selection_text = {
            "absolute_max": "绝对值最大值定位",
            "ultrasound_cross_modal": "\u6f0f\u78c1+\u8d85\u58f0\u8054\u5408\u786e\u8ba4",
            "configured_segment": "\u5f53\u524d\u6807\u5b9a\u5206\u6bb5",
            "global_multichannel": "\u5168\u6570\u636e\u591a\u901a\u9053\u626b\u63cf",
        }.get(str(result.get("selectionBasis", "")), "")
        ultrasound = result.get("ultrasound", {})
        if not ultrasound.get("available", False):
            ultrasound_text = "超声数据不可用"
        elif result.get("method") == "absolute_max":
            ultrasound_text = "超声数据已加载，但不参与定位"
        else:
            ultrasound_text = (
                "超声双 Track / 多 Gate 已辅助确认"
                if ultrasound.get("confidence") == "high"
                else "超声响应较弱，请人工确认"
            )
        cache_prefix = "已读取缓存 · " if from_cache else "已完成扫描 · "
        if result.get("method") == "absolute_max":
            absolute_value = result.get("absoluteValue", "—")
            channel = str(result.get("channel", "未知通道"))
            absolute_value_text = (
                f"{absolute_value:,}"
                if isinstance(absolute_value, (int, float))
                else str(absolute_value)
            )
            self.joint_detection_status = (
                f"{cache_prefix}{segment} record {record_pos:,}\n"
                f"|DAT.value| = {absolute_value_text} · 通道 {channel}\n"
                f"位置 {format_distance(x_m)} · 超声不参与定位"
            )
        else:
            self.joint_detection_status = (
                f"{cache_prefix}{segment} record {record_pos:,}，"
                f"MFL {method_text}"
                f"{(' / ' + selection_text) if selection_text else ''}"
                f"，{confidence_text}\n"
                f"位置 {format_distance(x_m)} · {ultrasound_text}"
            )
        target_start = clamp(
            x_m - self.window_width / 2.0,
            self.data_start,
            self.max_start(),
        )
        self.set_position(target_start)
        self.joint_label.setText(self.joint_detection_status)
        self.server_label.setText(
            "状态：接头定位完成" + ("（缓存）" if from_cache else "")
        )
        self.health_label.setText("接头定位完成")

    def on_joint_failed(self, message: str) -> None:
        self._set_joint_search_buttons_enabled(True)
        self.joint_detection_status = ""
        self.health_label.setText(message)
        self.joint_label.setText("定位失败：" + message)
        self._set_operation_status(message)

    def on_ultrasound_joint_progress(self, message: str) -> None:
        self.health_label.setText(message)
        self._set_operation_status(message)

    def on_ultrasound_joint_ready(self, result: dict[str, Any]) -> None:
        self._set_joint_search_buttons_enabled(True)
        try:
            candidate_id = int(result["candidateId"])
            current_id = int(result["currentJointId"])
            offset_m = float(result["offsetFromCurrentAnchorM"])
            gate_coverage = float(result["gateCoverage"])
            pair_coverage = float(result["gateTrackCoverage"])
            confidence = str(result.get("confidence", "low"))
            gates = [int(value) for value in result.get("gates", [])]
            tracks = [int(value) for value in result.get("tracks", [])]
            echo_count = int(result.get("echoCount", 0))
        except (KeyError, TypeError, ValueError) as exc:
            self.on_ultrasound_joint_failed(f"检测结果无效：{exc}")
            return

        confidence_text = {
            "high": "高",
            "medium": "中",
            "low": "低（建议谨慎确认）",
        }.get(confidence, confidence)
        prompt = (
            f"扫描候选超声接头 ID：{candidate_id:,}\n"
            f"当前配置 ID：{current_id:,}\n"
            f"相对当前锚点偏移：{offset_m:+.3f} m\n"
            f"匹配 Gate：{len(gates)}/9（{gate_coverage:.0%}；"
            f"{', '.join(map(str, gates)) or '无'}）\n"
            f"Track：{', '.join(map(str, tracks)) or '无'}；"
            f"Gate×Track 覆盖 {pair_coverage:.0%}；候选回波 {echo_count:,} 条\n"
            f"置信度：{confidence_text}\n\n"
            "算法依据局部多 Gate 覆盖和双 Track 回波簇给出候选，仍建议结合超声图像人工确认。\n"
            "确认后会将该 ID 保存到当前数据配置的 joint.us_id，并重新加载以更新坐标。"
        )
        default_button = (
            QMessageBox.Yes if confidence == "high" else QMessageBox.No
        )
        answer = QMessageBox.question(
            self,
            "确认超声接头候选",
            prompt,
            QMessageBox.Yes | QMessageBox.No,
            default_button,
        )
        if answer != QMessageBox.Yes:
            self.joint_label.setText(
                f"超声候选 ID {candidate_id:,} 未写入配置（置信度：{confidence_text}）"
            )
            self._set_operation_status("超声候选已保留，未修改当前配置")
            return

        try:
            config = _read_json_config(self.config_path)
            if not config:
                raise ValueError("当前数据配置为空或无法读取")
            joint = config.get("joint")
            if not isinstance(joint, dict):
                joint = {}
            joint["us_id"] = candidate_id
            config["joint"] = joint
            _write_json_config(self.config_path, config)
        except (OSError, ValueError, TypeError) as exc:
            self._set_joint_search_buttons_enabled(True)
            QMessageBox.warning(self, "保存超声接头失败", str(exc))
            return

        self.joint_label.setText(
            f"已确认超声接头 ID {candidate_id:,}，正在更新当前数据坐标…"
        )
        self._set_operation_status(
            f"超声接头 ID {candidate_id:,} 已写入当前数据配置，正在重载"
        )
        self._load_dataset_config(
            self.config_path,
            self.dataset_name,
            self.dataset_root,
        )

    def on_ultrasound_joint_failed(self, message: str) -> None:
        self._set_joint_search_buttons_enabled(True)
        self.health_label.setText(message)
        self.joint_label.setText(message)
        self._set_operation_status(message)

    def on_worker_failed(self, message: str) -> None:
        self.choose_dataset_button.setEnabled(True)
        self.fetch_pending = False
        if self.manifest is None:
            self.load_progress.setValue(0)
            self.load_progress.setFormat("加载失败")
        self.health_label.setText(message)
        self._set_operation_status("请检查 config.json、数据路径或后台读取错误")
        if self.manifest is None:
            self.play_button.setEnabled(False)

    def on_worker_progress(self, message: str) -> None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", message)
        if match:
            try:
                progress_percent = max(
                    0.0,
                    min(100.0, float(match.group(1))),
                )
            except ValueError:
                progress_percent = self.load_progress.value() / 10.0
            self.load_progress.setValue(int(round(progress_percent * 10)))
            self.load_progress.setFormat(
                f"加载中 {self.load_progress.value() / 10:.1f}%"
            )
        self.health_label.setText(message)
        self._set_operation_status(message)

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key_Space:
            self.toggle_play()
            event.accept()
            return
        if event.key() == Qt.Key_Left:
            self._handle_navigation_shortcut(
                -0.2 if event.modifiers() & Qt.ShiftModifier else -1.0
            )
            event.accept()
            return
        if event.key() == Qt.Key_Right:
            self._handle_navigation_shortcut(
                0.2 if event.modifiers() & Qt.ShiftModifier else 1.0
            )
            event.accept()
            return
        if event.key() == Qt.Key_Home:
            self._handle_navigation_shortcut(self.data_start, absolute=True)
            event.accept()
            return
        if event.key() == Qt.Key_End:
            self._handle_navigation_shortcut(self.max_start(), absolute=True)
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: Any) -> None:
        self.request_timer.stop()
        self.prefetch_timer.stop()
        self.cursor_timer.stop()
        self.play_timer.stop()
        for update_worker in (
            self.update_check_worker,
            self.update_download_worker,
        ):
            if update_worker is not None and update_worker.isRunning():
                update_worker.requestInterruption()
                if not update_worker.wait(35000):
                    QMessageBox.warning(
                        self,
                        "更新任务仍在运行",
                        "网络请求尚未结束，请稍后再关闭软件。",
                    )
                    event.ignore()
                    return
        # Closing must not leave a live QThread behind; Qt aborts the process
        # if a worker is destroyed while its run() method is still active.
        self.worker.stop(timeout=None)
        event.accept()


def configure_application(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(
        """
        QMainWindow, QWidget {
            background: #071019;
            color: #e7f0f5;
            font-family: "Segoe UI", "Microsoft YaHei";
            font-size: 10pt;
        }
        QFrame#HeaderFrame, QFrame#ControlFrame, QFrame#SidePanel {
            background: #0f1c29;
            border: 1px solid #223746;
            border-radius: 7px;
        }
        QLabel#BrandMark {
            color: #071019;
            background: #59d6e5;
            border: 1px solid #9af0f5;
            border-radius: 6px;
            font-size: 9pt;
            font-weight: 800;
            padding: 6px 8px;
        }
        QFrame#ActivityBar {
            background: #091520;
            border: 1px solid #203746;
            border-radius: 7px;
        }
        QLabel#ActivityLogo {
            color: #59d6e5;
            font-size: 18pt;
            font-weight: 700;
            padding: 2px;
        }
        QToolButton#ActivityButton {
            min-height: 60px;
            max-height: 60px;
            min-width: 42px;
            max-width: 42px;
            padding: 0;
            color: #78909e;
            background: transparent;
            border: 1px solid transparent;
            border-radius: 7px;
            font-size: 8pt;
        }
        QToolButton#ActivityButton:hover {
            color: #d5fcff;
            background: #122b39;
            border-color: #285466;
        }
        QToolButton#ActivityButton:checked {
            color: #69c5ff;
            background: #182c3c;
            border: 0;
            border-left: 2px solid #69c5ff;
        }
        QLabel#ActivityNote {
            color: #536d7b;
            font-size: 7pt;
            line-height: 1.1;
        }
        QFrame#SidebarHeader {
            background: transparent;
            border-bottom: 1px solid #203746;
        }
        QLabel#SidebarTitle {
            color: #e7f0f5;
            font-size: 13pt;
            font-weight: 700;
        }
        QLabel#SidebarHint {
            color: #78909e;
            font-size: 8pt;
        }
        QStackedWidget#ToolStack, QWidget#ToolPage {
            background: transparent;
            border: 0;
        }
        QFrame#WorkspaceFrame {
            background: #0b1722;
            border: 1px solid #223746;
            border-radius: 7px;
        }
        QFrame#WorkspaceToolbar {
            background: #0f1f2c;
            border: 1px solid #1d3544;
            border-radius: 6px;
        }
        QLabel#WorkspaceTitle {
            color: #e7f0f5;
            font-size: 11pt;
            font-weight: 700;
        }
        QLabel#WorkspaceSubtitle, QLabel#WorkspaceHint {
            color: #78909e;
            font-size: 8pt;
        }
        QLabel#DataTagUS, QLabel#DataTagMFL {
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 8pt;
            font-weight: 700;
        }
        QLabel#DataTagUS {
            color: #9af0f5;
            background: #123946;
            border: 1px solid #2e7782;
        }
        QLabel#DataTagMFL {
            color: #ffd1a4;
            background: #3b2b1f;
            border: 1px solid #8d633d;
        }
        QScrollArea#SidePanelScroll {
            background: #0f1c29;
            border: 1px solid #223746;
            border-radius: 7px;
        }
        QLabel { background: transparent; }
        QGroupBox {
            background: #0c1823;
            border: 1px solid #223746;
            border-radius: 6px;
            margin-top: 10px;
            padding-top: 8px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 9px;
            padding: 0 4px;
            color: #9fc0cf;
            font-size: 9pt;
            font-weight: 600;
        }
        QLabel#Eyebrow, QLabel#FilterLabel {
            color: #78909e;
            font-size: 8pt;
            font-weight: 600;
        }
        QLabel#Title {
            color: #e7f0f5;
            font-size: 16pt;
            font-weight: 700;
        }
        QLabel#Subtitle, QLabel#RangeLabel {
            color: #a9bdc9;
            font-size: 9pt;
        }
        QLabel#InfoLabel {
            color: #a9bdc9;
            font-size: 8pt;
        }
        QLabel#InfoSection {
            color: #78c9d5;
            font-size: 8pt;
            font-weight: 700;
            padding-top: 4px;
            padding-bottom: 1px;
            border-bottom: 1px solid #223746;
        }
        QLabel#JointStatus {
            color: #d9f3d8;
            background: #122a27;
            border: 1px solid #2e6e63;
            border-radius: 4px;
            padding: 5px 6px;
            font-size: 8pt;
        }
        QLabel#Health {
            color: #83dc9d;
            font-size: 9pt;
            padding: 5px;
        }
        QProgressBar#LoadProgress {
            min-height: 18px;
            max-height: 18px;
            color: #d8fcff;
            background: #0d1b26;
            border: 1px solid #2b4657;
            border-radius: 4px;
            text-align: center;
        }
        QProgressBar#LoadProgress::chunk {
            background: #2e9da8;
            border-radius: 3px;
        }
        QPushButton, QToolButton, QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit {
            min-height: 26px;
            padding: 3px 9px;
            color: #e7f0f5;
            background: #142838;
            border: 1px solid #2b4657;
            border-radius: 5px;
        }
        QPushButton:hover, QToolButton:hover, QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover, QLineEdit:hover {
            border-color: #59d6e5;
            background: #183548;
        }
        QPushButton:disabled, QToolButton:disabled, QComboBox:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled, QLineEdit:disabled {
            color: #627887;
            background: #0d1b26;
            border-color: #1c303d;
        }
        QPushButton#PrimaryButton {
            color: #d8fcff;
            border-color: #3f9ba7;
            background: #17404a;
            font-weight: 700;
        }
        QCheckBox {
            spacing: 5px;
            color: #a9bdc9;
        }
        QCheckBox::indicator {
            width: 13px;
            height: 13px;
        }
        QCheckBox::indicator:checked {
            background: #59d6e5;
            border: 1px solid #8be8ef;
        }
        QToolButton:checked {
            color: #071019;
            background: #59d6e5;
            border-color: #9af0f5;
        }
        QScrollArea#GateScroll {
            background: #0a1722;
            border: 1px solid #1e3544;
            border-radius: 5px;
        }
        QListWidget {
            background: #0a1722;
            color: #c6d7df;
            border: 1px solid #1e3544;
            border-radius: 5px;
            padding: 2px;
        }
        QListWidget::item {
            padding: 3px 2px;
        }
        QListWidget::item:selected {
            color: #071019;
            background: #59d6e5;
        }
        QSlider::groove:horizontal {
            height: 4px;
            background: #203b4b;
            border-radius: 2px;
        }
        QSlider::handle:horizontal {
            width: 13px;
            margin: -5px 0;
            border-radius: 7px;
            background: #59d6e5;
        }
        QSplitter::handle {
            background: #213c4b;
        }
        """
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="原生 Qt 双模态数据回放")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="只初始化数据并读取接头窗口，不打开 GUI",
    )
    return parser.parse_args()


def run_smoke_test(config_path: Path) -> int:
    service = ReplayService(config_path.resolve())
    manifest = service.manifest()
    payload = service.window(
        -4.0,
        4.0,
        tracks=[0, 1],
        gates=None,
        max_mfl_points=180,
        max_us_records=12000,
    )
    model_rows = 0
    model_anomalies = 0
    raw_model_path = service.config.get("model_result_path")
    if raw_model_path:
        model_path = Path(str(raw_model_path))
        if not model_path.is_absolute():
            model_path = service.config_path.parent / model_path
        if model_path.exists():
            model_store = ModelResultStore(
                model_path,
                service.mfl,
                default_segment=str(
                    service.config.get("model_result_segment", "EX01")
                ),
            )
            model_rows = len(model_store.predictions)
            model_anomalies = sum(
                1 for item in model_store.predictions if item.get("is_anomaly")
            )
    print(
        "desktop smoke test ok: "
        f"mfl_rows={len(payload['mfl']['rows'])}, "
        f"mfl_segments={len(manifest['mfl']['segments'])}, "
        f"us_count={payload['ultrasound']['count']}, "
        f"us_total={manifest['ultrasound'].get('totalRecords', 0)}, "
        f"model_rows={model_rows}, model_anomalies={model_anomalies}"
    )
    return 0


def main() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    args = parse_args()
    config_path = args.config
    if not config_path.is_absolute():
        config_path = (APP_DIR / config_path).resolve()
    if args.smoke_test:
        return run_smoke_test(config_path)

    app = QApplication(sys.argv)
    configure_application(app)
    window = ReplayWindow(config_path)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
