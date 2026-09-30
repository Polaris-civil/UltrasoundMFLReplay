"""Load model-generated MFL anomaly events and project them onto the replay axis.

The model pipeline and the desktop replay program intentionally keep separate
files.  This adapter understands the current event outputs and turns their
local record ranges into the same joint-relative metre coordinates used by
the replay window.  Semantic labels from the legacy rule classifier are
intentionally ignored until a reliable labelled model is available.  The
optional ``frequency_hint`` fields from the sliding-window frequency detector
are shown as provisional hints, never as saved human truth labels.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Any

import numpy as np


MODEL_ANOMALY_CODE = "model_anomaly"
MODEL_ANOMALY_NAME = "模型异常候选"
MODEL_ANOMALY_COLOR = "#FF66B3"
FREQUENCY_HINTS = {
    "vibration_interference": ("振动干扰", "#FFB000"),
    "weld": ("焊缝", "#B56CFF"),
    "suspected_oblique_crack": ("疑似斜裂纹", "#FF507A"),
}


def _int_value(row: dict[str, str], key: str) -> int:
    return int(float(str(row.get(key, "")).strip()))


def _float_value(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        value = float(str(row.get(key, "")).strip())
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _scope(row: dict[str, str], classified: bool) -> str:
    if not classified:
        value = str(row.get("rail", "")).strip().lower()
        return value if value in {"left", "right"} else "all"
    value = str(row.get("dominant_side", "")).strip().lower()
    if value in {"left", "right"}:
        return value
    return "all"


class ModelResultStore:
    """Read current model event CSVs and map them to replay coordinates."""

    def __init__(
        self,
        path: str | Path,
        mfl_dataset: Any,
        default_segment: str = "EX01",
    ) -> None:
        self.path = Path(path).resolve()
        self.mfl_dataset = mfl_dataset
        self.default_segment = default_segment
        self.format_name = ""
        self.predictions: list[dict[str, Any]] = []
        self._raw_index_cache: dict[tuple[str, int], int] = {}
        self._load()

    def _load(self) -> None:
        pending_rows: list[tuple[dict[str, str], int, bool]] = []
        positions_by_segment: dict[str, list[int]] = {}
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or ())
            classified = "predicted_label" in fields
            candidate = "peak_anomaly_score" in fields
            if not classified and not candidate:
                raise ValueError(
                    "模型结果缺少 predicted_label 或 peak_anomaly_score 字段"
                )
            if "frequency_hint" in fields:
                self.format_name = "滑动窗口频率提示（暂定）"
            else:
                self.format_name = (
                    "异常候选结果（不使用类别）"
                    if classified
                    else "异常评分结果"
                )
            for row_number, row in enumerate(reader, 1):
                row_dict = dict(row)
                pending_rows.append((row_dict, row_number, classified))
                try:
                    start_record = _int_value(row_dict, "start_record")
                    end_record = _int_value(row_dict, "end_record")
                except (KeyError, TypeError, ValueError):
                    continue
                if end_record < start_record:
                    start_record, end_record = end_record, start_record
                segment_name = str(
                    row_dict.get("mfl_segment") or self.default_segment
                ).strip()
                segment = getattr(
                    self.mfl_dataset, "segment_by_name", {}
                ).get(segment_name)
                if segment is None:
                    raise ValueError(
                        f"模型结果第 {row_number} 行引用了不存在的漏磁段："
                        f"{segment_name}"
                    )
                if not 0 <= start_record < segment.count:
                    continue
                end_record = min(end_record, segment.count - 1)
                if end_record >= start_record:
                    positions_by_segment.setdefault(segment_name, []).extend(
                        (start_record, end_record)
                    )

        # Fetch all source indices for a segment in one NumPy operation. The
        # result files can contain thousands of events; scalar memmap reads
        # make startup unnecessarily slow on a spinning disk.
        for segment_name, positions in positions_by_segment.items():
            segment = self.mfl_dataset.segment_by_name[segment_name]
            unique_positions = np.asarray(sorted(set(positions)), dtype=np.int64)
            if unique_positions.size == 0:
                continue
            raw_values = np.asarray(
                segment.channels["leftxchannel1"]["index"][unique_positions],
                dtype=np.int64,
            )
            self._raw_index_cache.update(
                ((segment_name, int(position)), int(raw_index))
                for position, raw_index in zip(unique_positions, raw_values)
            )

        for row_dict, row_number, classified in pending_rows:
            prediction = self._convert_row(
                row_dict,
                row_number=row_number,
                classified=classified,
            )
            if prediction is not None:
                self.predictions.append(prediction)

        self.predictions.sort(
            key=lambda item: (
                float(item["x_start_m"]),
                float(item["x_end_m"]),
                str(item["prediction_id"]),
            )
        )

    def _convert_row(
        self,
        row: dict[str, str],
        *,
        row_number: int,
        classified: bool,
    ) -> dict[str, Any] | None:
        try:
            start_record = _int_value(row, "start_record")
            end_record = _int_value(row, "end_record")
        except (KeyError, TypeError, ValueError):
            return None
        if end_record < start_record:
            start_record, end_record = end_record, start_record

        segment_name = str(
            row.get("mfl_segment") or self.default_segment
        ).strip()
        segment = getattr(self.mfl_dataset, "segment_by_name", {}).get(segment_name)
        if segment is None:
            raise ValueError(
                f"模型结果第 {row_number} 行引用了不存在的漏磁段：{segment_name}"
            )
        if not 0 <= start_record < segment.count:
            return None
        end_record = min(end_record, segment.count - 1)
        if end_record < start_record:
            return None

        reference = segment.channels["leftxchannel1"]
        raw_start = self._raw_index_cache.get((segment_name, start_record))
        raw_end = self._raw_index_cache.get((segment_name, end_record))
        if raw_start is None:
            raw_start = int(reference["index"][start_record])
        if raw_end is None:
            raw_end = int(reference["index"][end_record])
        global_start = (
            segment.global_start_units
            + raw_start
            - segment.first_index
        )
        global_end = (
            segment.global_start_units
            + raw_end
            - segment.first_index
        )
        x_values = self.mfl_dataset.x_from_global_units(
            [global_start, global_end]
        )
        x_start_m = float(min(x_values))
        x_end_m = float(max(x_values))

        label_code = MODEL_ANOMALY_CODE
        label_name = MODEL_ANOMALY_NAME
        frequency_hint = str(row.get("frequency_hint", "")).strip()
        frequency_hint_name = str(row.get("frequency_hint_name", "")).strip()
        frequency_color = MODEL_ANOMALY_COLOR
        if frequency_hint in FREQUENCY_HINTS:
            default_name, frequency_color = FREQUENCY_HINTS[frequency_hint]
            frequency_hint_name = frequency_hint_name or default_name
            label_name = default_name + "候选"
        if classified:
            # Keep the old field only for excluding rows explicitly marked
            # normal in a legacy CSV.  Never expose or assign its semantic
            # category in the replay UI at this stage.
            raw_label = str(row.get("predicted_label", "")).strip()
            is_anomaly = raw_label not in {"", "正常"}
            confidence = _float_value(row, "confidence")
            score = _float_value(row, "anomaly_score")
        else:
            is_anomaly = True
            confidence = _float_value(row, "peak_anomaly_score")
            score = confidence
        prediction_id = str(row.get("event_id") or f"row_{row_number:06d}")

        return {
            "prediction_id": prediction_id,
            "label": label_code,
            "label_name": label_name,
            "is_anomaly": is_anomaly,
            "scope": _scope(row, classified),
            "x_start_m": x_start_m,
            "x_end_m": x_end_m,
            "start_record": start_record,
            "end_record": end_record,
            "start_dat_index": raw_start,
            "end_dat_index": raw_end,
            "confidence": confidence,
            "score": score,
            "confidence_level": str(row.get("confidence_level", "")).strip(),
            "frequency_hint": frequency_hint,
            "frequency_hint_name": frequency_hint_name,
            "frequency_peak_cpr": _float_value(row, "frequency_peak_cpr"),
            "frequency_confidence": _float_value(
                row,
                "frequency_confidence",
            ),
            "frequency_change_direction": str(
                row.get("frequency_change_direction", "")
            ).strip(),
            "reason": str(
                row.get("matched_rules")
                or row.get("reason_counts")
                or row.get("candidate_reason")
                or ""
            ).strip(),
            "source_row": row,
            "color": frequency_color,
        }
