"""Human annotation persistence for the synchronized replay application.

The annotation files deliberately keep references to the original MFL DAT
records instead of copying the raw signal into a CSV.  This makes a label
stable, auditable, and cheap to version while still allowing a later training
pipeline to materialize the exact 32-channel slice on demand.
"""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np


MFL_RECORD_DTYPE = np.dtype(
    [
        ("index", "<i4"),
        ("value", "<i2"),
    ],
    align=False,
)
MFL_CHANNELS = tuple(
    f"{side}{axis}channel{number}"
    for side in ("left", "right")
    for number in range(1, 9)
    for axis in ("x", "z")
)

ANNOTATION_LABELS = {
    "weld": "焊缝",
    "diagonal_crack": "斜裂纹",
    "vibration_interference": "振动干扰",
    "suspected_diagonal_crack": "疑似斜裂纹",
    "unknown_anomaly": "未知异常",
}
EXCLUSION_REASONS = {
    "joint": "接头/拼接区域",
    "acquisition_fault": "采集故障",
    "out_of_scope": "暂不纳入判定",
    "other": "其他排除原因",
}

EVENT_FIELDS = (
    "annotation_id",
    "span_index",
    "dataset",
    "label",
    "label_name",
    "mfl_segment",
    "start_record_pos",
    "end_record_pos",
    "start_dat_index",
    "end_dat_index",
    "x_start_m",
    "x_end_m",
    "selected_x_start_m",
    "selected_x_end_m",
    "rail_scope",
    "direction_scope",
    "channel_scope",
    "source_window_start_m",
    "source_window_end_m",
    "us_alignment_offset_m",
    "created_at",
    "annotator",
    "notes",
)
REVIEW_FIELDS = (
    "review_id",
    "span_index",
    "dataset",
    "mfl_segment",
    "start_record_pos",
    "end_record_pos",
    "start_dat_index",
    "end_dat_index",
    "x_start_m",
    "x_end_m",
    "selected_x_start_m",
    "selected_x_end_m",
    "reviewed_at",
    "reviewed_by",
    "source",
)
EXCLUSION_FIELDS = (
    "exclusion_id",
    "span_index",
    "dataset",
    "mfl_segment",
    "start_record_pos",
    "end_record_pos",
    "start_dat_index",
    "end_dat_index",
    "x_start_m",
    "x_end_m",
    "selected_x_start_m",
    "selected_x_end_m",
    "reason",
    "reason_name",
    "created_at",
    "created_by",
    "notes",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _format_float(value: Any, digits: int = 6) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:.{digits}f}"


def _resolve_path(value: str | Path, base: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def _portable_relative_path(target: Path, base: Path) -> str:
    """Store a path that remains valid when the distribution is moved."""
    try:
        return Path(os.path.relpath(str(target), start=str(base))).as_posix()
    except ValueError:
        # Windows cannot form a relative path across drive letters. This is
        # only a fallback for custom cross-drive annotation locations; the
        # bundled distribution keeps data and annotations on one tree.
        return str(target)


def _natural_id_number(value: Any, prefix: str) -> int:
    match = re.fullmatch(rf"{re.escape(prefix)}(\d+)", str(value or ""))
    return int(match.group(1)) if match else 0


class AnnotationStore:
    """Read and write versioned annotation references for one dataset."""

    schema_version = 1

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path).resolve()
        self.config = json.loads(
            self.config_path.read_text(encoding="utf-8-sig")
        )
        self.dataset = str(self.config.get("dataset", "20251221"))
        annotation_dir = self.config.get(
            "annotation_dir", "分发软件版本/annotations"
        )
        self.root_dir = _resolve_path(annotation_dir, self.config_path.parent)
        self.dataset_dir = self.root_dir / self.dataset
        self.mfl_data_dir = _resolve_path(
            self.config.get(
                "mfl_data_dir", "分发软件版本/data/20251221/漏磁"
            ),
            self.config_path.parent,
        )

    @property
    def event_path(self) -> Path:
        return self.dataset_dir / "event_labels.csv"

    @property
    def reviewed_path(self) -> Path:
        return self.dataset_dir / "reviewed_ranges.csv"

    @property
    def exclusion_path(self) -> Path:
        return self.dataset_dir / "excluded_ranges.csv"

    @property
    def manifest_path(self) -> Path:
        return self.dataset_dir / "manifest.json"

    def _read_csv(self, path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    def _write_csv(
        self,
        path: Path,
        fieldnames: Iterable[str],
        rows: Iterable[dict[str, Any]],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8-sig",
                newline="",
                delete=False,
                dir=str(path.parent),
                prefix=f".{path.stem}.",
                suffix=".tmp",
            ) as handle:
                temporary_path = handle.name
                writer = csv.DictWriter(
                    handle,
                    fieldnames=list(fieldnames),
                    extrasaction="ignore",
                )
                writer.writeheader()
                for row in rows:
                    writer.writerow(
                        {
                            key: "" if row.get(key) is None else row.get(key)
                            for key in fieldnames
                        }
                    )
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def _next_id(
        self,
        rows: Iterable[dict[str, Any]],
        field: str,
        prefix: str,
    ) -> str:
        latest = max(
            (_natural_id_number(row.get(field), prefix) for row in rows),
            default=0,
        )
        return f"{prefix}{latest + 1:06d}"

    def _write_manifest(self) -> None:
        self.dataset_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "dataset": self.dataset,
            "source_config": _portable_relative_path(
                self.config_path,
                self.dataset_dir,
            ),
            "mfl_data_dir": _portable_relative_path(
                self.mfl_data_dir,
                self.dataset_dir,
            ),
            "raw_record_dtype": [
                [name, str(dtype)] for name, (dtype, _offset) in MFL_RECORD_DTYPE.fields.items()
            ],
            "record_bytes": int(MFL_RECORD_DTYPE.itemsize),
            "channel_order": list(MFL_CHANNELS),
            "coordinate": {
                "scale_mm_per_unit": self.config.get("scale_mm_per_unit"),
                "direction_sign": self.config.get("direction_sign", 1),
                "joint": self.config.get("joint", {}),
            },
            "created_at": _now_iso(),
        }
        self.manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _span_coordinates(span: dict[str, Any]) -> tuple[float, float]:
        start = float(span.get("xStartM", span.get("x_start_m")))
        end = float(span.get("xEndM", span.get("x_end_m")))
        return min(start, end), max(start, end)

    def load_event_rows(self) -> list[dict[str, str]]:
        return self._read_csv(self.event_path)

    def load_event_groups(self) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for row in self.load_event_rows():
            event_id = str(row.get("annotation_id", ""))
            if not event_id:
                continue
            group = groups.setdefault(
                event_id,
                {
                    "annotation_id": event_id,
                    "label": row.get("label", ""),
                    "label_name": row.get("label_name", ""),
                    "selected_x_start_m": row.get("selected_x_start_m", ""),
                    "selected_x_end_m": row.get("selected_x_end_m", ""),
                    "created_at": row.get("created_at", ""),
                    "spans": [],
                },
            )
            group["spans"].append(row)
        return list(groups.values())

    def save_event(
        self,
        spans: list[dict[str, Any]],
        *,
        selected_start_m: float,
        selected_end_m: float,
        label: str,
        rail_scope: str = "all",
        direction_scope: str = "xz",
        channel_scope: str = "all",
        source_window_start_m: float | None = None,
        source_window_end_m: float | None = None,
        us_alignment_offset_m: float = 0.0,
        annotator: str = "",
        notes: str = "",
        annotation_id: str | None = None,
    ) -> str:
        if label not in ANNOTATION_LABELS:
            raise ValueError(f"不支持的异常类型：{label}")
        if not spans:
            raise ValueError("所选区间没有对应的漏磁原始记录")

        rows = self.load_event_rows()
        if annotation_id:
            annotation_id = str(annotation_id)
            if not any(
                str(row.get("annotation_id", "")) == annotation_id
                for row in rows
            ):
                raise ValueError(f"未找到待更新的标注：{annotation_id}")
            output_rows = [
                row
                for row in rows
                if str(row.get("annotation_id", "")) != annotation_id
            ]
            existing_created_at = next(
                (
                    str(row.get("created_at", ""))
                    for row in rows
                    if str(row.get("annotation_id", "")) == annotation_id
                    and row.get("created_at")
                ),
                "",
            )
        else:
            annotation_id = self._next_id(rows, "annotation_id", "EV")
            output_rows = list(rows)
            existing_created_at = ""
        selected_low = min(float(selected_start_m), float(selected_end_m))
        selected_high = max(float(selected_start_m), float(selected_end_m))
        timestamp = existing_created_at or _now_iso()
        for span_index, span in enumerate(spans):
            x_start, x_end = self._span_coordinates(span)
            output_rows.append(
                {
                    "annotation_id": annotation_id,
                    "span_index": span_index,
                    "dataset": self.dataset,
                    "label": label,
                    "label_name": ANNOTATION_LABELS[label],
                    "mfl_segment": str(span["name"]),
                    "start_record_pos": int(span["recordStart"]),
                    "end_record_pos": int(span["recordEnd"]),
                    "start_dat_index": int(span["sourceIndexStart"]),
                    "end_dat_index": int(span["sourceIndexEnd"]),
                    "x_start_m": _format_float(x_start),
                    "x_end_m": _format_float(x_end),
                    "selected_x_start_m": _format_float(selected_low),
                    "selected_x_end_m": _format_float(selected_high),
                    "rail_scope": rail_scope,
                    "direction_scope": direction_scope,
                    "channel_scope": channel_scope,
                    "source_window_start_m": _format_float(source_window_start_m),
                    "source_window_end_m": _format_float(source_window_end_m),
                    "us_alignment_offset_m": _format_float(us_alignment_offset_m, 3),
                    "created_at": timestamp,
                    "annotator": annotator,
                    "notes": notes,
                }
            )
        self._write_csv(self.event_path, EVENT_FIELDS, output_rows)
        self._write_manifest()
        return annotation_id

    def save_reviewed_range(
        self,
        spans: list[dict[str, Any]],
        *,
        selected_start_m: float,
        selected_end_m: float,
        reviewed_by: str = "",
        source: str = "manual_replay",
    ) -> str:
        if not spans:
            raise ValueError("所选区间没有对应的漏磁原始记录")
        rows = self._read_csv(self.reviewed_path)
        review_id = self._next_id(rows, "review_id", "RV")
        selected_low = min(float(selected_start_m), float(selected_end_m))
        selected_high = max(float(selected_start_m), float(selected_end_m))
        timestamp = _now_iso()
        output_rows = list(rows)
        for span_index, span in enumerate(spans):
            x_start, x_end = self._span_coordinates(span)
            output_rows.append(
                {
                    "review_id": review_id,
                    "span_index": span_index,
                    "dataset": self.dataset,
                    "mfl_segment": str(span["name"]),
                    "start_record_pos": int(span["recordStart"]),
                    "end_record_pos": int(span["recordEnd"]),
                    "start_dat_index": int(span["sourceIndexStart"]),
                    "end_dat_index": int(span["sourceIndexEnd"]),
                    "x_start_m": _format_float(x_start),
                    "x_end_m": _format_float(x_end),
                    "selected_x_start_m": _format_float(selected_low),
                    "selected_x_end_m": _format_float(selected_high),
                    "reviewed_at": timestamp,
                    "reviewed_by": reviewed_by,
                    "source": source,
                }
            )
        self._write_csv(self.reviewed_path, REVIEW_FIELDS, output_rows)
        self._write_manifest()
        return review_id

    def save_exclusion(
        self,
        spans: list[dict[str, Any]],
        *,
        selected_start_m: float,
        selected_end_m: float,
        reason: str,
        created_by: str = "",
        notes: str = "",
    ) -> str:
        if reason not in EXCLUSION_REASONS:
            raise ValueError(f"不支持的排除原因：{reason}")
        if not spans:
            raise ValueError("所选区间没有对应的漏磁原始记录")
        rows = self._read_csv(self.exclusion_path)
        exclusion_id = self._next_id(rows, "exclusion_id", "EXC")
        selected_low = min(float(selected_start_m), float(selected_end_m))
        selected_high = max(float(selected_start_m), float(selected_end_m))
        timestamp = _now_iso()
        output_rows = list(rows)
        for span_index, span in enumerate(spans):
            x_start, x_end = self._span_coordinates(span)
            output_rows.append(
                {
                    "exclusion_id": exclusion_id,
                    "span_index": span_index,
                    "dataset": self.dataset,
                    "mfl_segment": str(span["name"]),
                    "start_record_pos": int(span["recordStart"]),
                    "end_record_pos": int(span["recordEnd"]),
                    "start_dat_index": int(span["sourceIndexStart"]),
                    "end_dat_index": int(span["sourceIndexEnd"]),
                    "x_start_m": _format_float(x_start),
                    "x_end_m": _format_float(x_end),
                    "selected_x_start_m": _format_float(selected_low),
                    "selected_x_end_m": _format_float(selected_high),
                    "reason": reason,
                    "reason_name": EXCLUSION_REASONS[reason],
                    "created_at": timestamp,
                    "created_by": created_by,
                    "notes": notes,
                }
            )
        self._write_csv(self.exclusion_path, EXCLUSION_FIELDS, output_rows)
        self._write_manifest()
        return exclusion_id

    def delete_event(self, annotation_id: str) -> bool:
        rows = self.load_event_rows()
        remaining = [
            row for row in rows
            if str(row.get("annotation_id", "")) != str(annotation_id)
        ]
        if len(remaining) == len(rows):
            return False
        self._write_csv(self.event_path, EVENT_FIELDS, remaining)
        self._write_manifest()
        return True

    def read_event_values(self, annotation_id: str) -> dict[str, Any]:
        """Read exact raw MFL values referenced by one annotation.

        The return value contains one item per EX segment.  A label crossing a
        segment boundary therefore remains lossless and does not pretend that
        two independent DAT files are one physically continuous mmap.
        """
        rows = [
            row
            for row in self.load_event_rows()
            if str(row.get("annotation_id", "")) == str(annotation_id)
        ]
        if not rows:
            raise KeyError(f"未找到标注：{annotation_id}")
        rows.sort(key=lambda row: int(row.get("span_index", 0) or 0))
        span_values: list[dict[str, Any]] = []
        for row in rows:
            segment = str(row["mfl_segment"])
            start = int(row["start_record_pos"])
            stop = int(row["end_record_pos"]) + 1
            segment_dir = self.mfl_data_dir / segment
            reference_path = segment_dir / "leftxchannel1.dat"
            reference = np.memmap(reference_path, dtype=MFL_RECORD_DTYPE, mode="r")
            if start < 0 or stop > len(reference) or stop <= start:
                raise ValueError(
                    f"标注 {annotation_id} 的 {segment} 记录范围无效：{start}..{stop - 1}"
                )
            indices = np.asarray(reference[start:stop]["index"], dtype=np.int32)
            values: list[np.ndarray] = []
            for channel in MFL_CHANNELS:
                path = segment_dir / f"{channel}.dat"
                array = np.memmap(path, dtype=MFL_RECORD_DTYPE, mode="r")
                values.append(np.asarray(array[start:stop]["value"], dtype=np.int16))
            span_values.append(
                {
                    "segment": segment,
                    "start_record_pos": start,
                    "end_record_pos": stop - 1,
                    "dat_index": indices,
                    "channel_order": list(MFL_CHANNELS),
                    "values": np.stack(values, axis=0),
                }
            )
        return {
            "annotation_id": str(annotation_id),
            "label": rows[0].get("label", ""),
            "label_name": rows[0].get("label_name", ""),
            "spans": span_values,
        }
