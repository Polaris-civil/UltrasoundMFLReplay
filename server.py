from __future__ import annotations

import argparse
import json
import math
import mimetypes
import re
import time
import traceback
import webbrowser
from collections import OrderedDict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ultrasound_converter import ensure_binary_data


APP_DIR = Path(__file__).resolve().parent
MFL_RECORD_DTYPE = np.dtype(
    [
        ("index", "<i4"),
        ("value", "<i2"),
    ],
    align=False,
)
US_RECORD_DTYPE = np.dtype(
    [
        ("mileage", "<f4"),
        ("id", "<u4"),
        ("depth", "u1"),
        ("gate", "u1"),
        ("track", "u1"),
        ("segment", "u1"),
    ],
    align=False,
)
SIDES = ("left", "right")
AXES = ("x", "z")
CHANNELS = tuple(
    f"{side}{axis}channel{number}"
    for side in SIDES
    for number in range(1, 9)
    for axis in AXES
)
DISPLAY_ROWS = tuple(
    (side, number)
    for side in SIDES
    for number in range(1, 9)
)
DEFAULT_SCALE_MM = 0.57462
DEFAULT_MFL_MAX_POINTS = 3600
DEFAULT_US_MAX_RECORDS = 120_000
MAX_DETAIL_WINDOW_M = 200.0
JOINT_BOUNDARY_VALUES = (-10_000, 9_995)
JOINT_SCAN_SAMPLE_STRIDE = 32
JOINT_SCAN_WINDOW_RECORDS = 1_024
JOINT_SCAN_BLOCK_RECORDS = 1_048_576
JOINT_ROBUST_Z = 8.0
JOINT_US_GATES = frozenset((4, 5, 6, 10, 11, 12, 15, 19, 20))


class DataLoadCancelled(Exception):
    """Internal signal used to stop a large data load safely."""


def resolve_path(value: str | Path, base: Path = APP_DIR) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base / path).resolve()


def natural_segment_key(path: Path) -> tuple[int, str]:
    match = re.search(r"(\d+)$", path.name)
    return (int(match.group(1)) if match else 10**9, path.name)


def sample_is_non_decreasing(values: np.ndarray, sample_count: int = 2048) -> bool:
    if len(values) < 2:
        return True
    positions = np.linspace(
        0,
        len(values) - 1,
        num=min(sample_count, len(values)),
        dtype=np.int64,
    )
    sampled = np.asarray(values[positions])
    return bool(np.all(np.diff(sampled) >= 0))


def downsample_pair(
    x: np.ndarray,
    x_values: np.ndarray,
    z_values: np.ndarray,
    max_points: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """保留每个桶的首尾和 X/Z 极值，避免窄峰被平均掉。"""
    count = len(x)
    if count <= max_points:
        return x, x_values, z_values

    max_points = max(32, int(max_points))
    bucket_count = max(1, max_points // 4)
    bucket_size = max(1, int(math.ceil(count / bucket_count)))
    selected: list[int] = []

    for start in range(0, count, bucket_size):
        stop = min(count, start + bucket_size)
        if stop <= start:
            continue
        local = [start, stop - 1]
        if stop - start > 1:
            x_block = x_values[start:stop]
            z_block = z_values[start:stop]
            local.extend(
                [
                    start + int(np.argmin(x_block)),
                    start + int(np.argmax(x_block)),
                    start + int(np.argmin(z_block)),
                    start + int(np.argmax(z_block)),
                ]
            )
        selected.extend(local)

    indices = np.unique(np.asarray(selected, dtype=np.int64))
    if len(indices) > max_points:
        keep = np.linspace(0, len(indices) - 1, max_points, dtype=np.int64)
        indices = indices[keep]

    return x[indices], x_values[indices], z_values[indices]


def downsample_memmap_pair(
    reference_index: np.ndarray,
    x_value_source: np.ndarray,
    z_value_source: np.ndarray,
    start: int,
    stop: int,
    global_start_units: float,
    first_index: int,
    joint_global_units: float,
    scale_m_per_unit: float,
    direction_sign: int,
    max_points: int | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Downsample a DAT window without materializing the whole window twice."""
    count = max(0, int(stop) - int(start))
    if count == 0:
        empty = np.asarray([], dtype=np.float32)
        return empty, empty, empty

    if max_points is not None:
        max_points = max(32, int(max_points))
    if max_points is None or count <= max_points:
        positions = np.arange(start, stop, dtype=np.int64)
    else:
        bucket_count = max(1, max_points // 4)
        bucket_size = max(1, int(math.ceil(count / bucket_count)))
        selected: list[int] = []
        for local_start in range(0, count, bucket_size):
            local_stop = min(count, local_start + bucket_size)
            if local_stop <= local_start:
                continue
            selected.extend((local_start, local_stop - 1))
            if local_stop - local_start > 1:
                x_block = np.asarray(
                    x_value_source[start + local_start : start + local_stop]
                )
                z_block = np.asarray(
                    z_value_source[start + local_start : start + local_stop]
                )
                selected.extend(
                    (
                        local_start + int(np.argmin(x_block)),
                        local_start + int(np.argmax(x_block)),
                        local_start + int(np.argmin(z_block)),
                        local_start + int(np.argmax(z_block)),
                    )
                )
        indices = np.unique(np.asarray(selected, dtype=np.int64))
        if len(indices) > max_points:
            keep = np.linspace(0, len(indices) - 1, max_points, dtype=np.int64)
            indices = indices[keep]
        positions = start + indices

    raw_indices = np.asarray(reference_index[positions], dtype=np.float32)
    global_units = (
        float(global_start_units)
        + raw_indices.astype(np.float64)
        - float(first_index)
    )
    x = (
        (global_units - float(joint_global_units))
        * float(scale_m_per_unit)
        * int(direction_sign)
    ).astype(np.float32)
    x_values = (
        np.asarray(x_value_source[positions], dtype=np.float32) / 1000.0
    )
    z_values = (
        np.asarray(z_value_source[positions], dtype=np.float32) / 1000.0
    )
    return x, x_values, z_values


@dataclass
class MFLSegment:
    name: str
    path: Path
    channels: dict[str, np.memmap]
    count: int
    first_index: int
    last_index: int
    global_start_units: float
    global_end_units: float
    index_monotonic: bool
    gap_before_m: float


class MFLDataset:
    """读取 EX01～EX04，并将局部 DAT index 映射到连续坐标。"""

    def __init__(
        self,
        data_dir: Path,
        scale_m_per_unit: float,
        segment_gaps_m: dict[str, float],
        joint_config: dict[str, Any],
        direction_sign: int = 1,
        stop_callback: Callable[[], bool] | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.scale_m_per_unit = float(scale_m_per_unit)
        self.direction_sign = 1 if int(direction_sign) >= 0 else -1
        self.segments: list[MFLSegment] = []
        self.warnings: list[str] = []

        segment_dirs = sorted(
            [
                path
                for path in data_dir.iterdir()
                if path.is_dir() and re.fullmatch(r"EX\d+", path.name, re.IGNORECASE)
            ],
            key=natural_segment_key,
        )
        if not segment_dirs:
            raise FileNotFoundError(f"未找到 EX01～EXnn 目录：{data_dir}")

        next_global_start = 0.0
        total_segments = len(segment_dirs)
        channels_per_segment = len(SIDES) * len(AXES) * 8
        for segment_number, segment_dir in enumerate(segment_dirs, start=1):
            if stop_callback is not None and stop_callback():
                raise DataLoadCancelled()
            segment_progress_start = (
                20.0 + 65.0 * (segment_number - 1) / total_segments
            )
            segment_progress_end = (
                20.0 + 65.0 * segment_number / total_segments
            )
            segment_progress_width = (
                segment_progress_end - segment_progress_start
            )
            map_progress_share = 0.92
            if progress_callback is not None:
                progress_callback(
                    f"加载进度 {segment_progress_start:.1f}%：正在读取漏磁 {segment_dir.name} "
                    f"（第 {segment_number}/{total_segments} 段）"
                )
            channels: dict[str, np.memmap] = {}
            missing: list[str] = []
            lengths: set[int] = set()

            for side_index, side in enumerate(SIDES):
                for axis_index, axis in enumerate(AXES):
                    for number in range(1, 9):
                        if stop_callback is not None and stop_callback():
                            raise DataLoadCancelled()
                        key = f"{side}{axis}channel{number}"
                        path = segment_dir / f"{key}.dat"
                        channel_offset = (
                            (side_index * len(AXES) + axis_index) * 8
                            + (number - 1)
                        )
                        channel_progress_width = (
                            segment_progress_width
                            * map_progress_share
                            / channels_per_segment
                        )
                        start_progress = (
                            segment_progress_start
                            + segment_progress_width
                            * map_progress_share
                            * channel_offset
                            / channels_per_segment
                        )
                        end_progress = start_progress + channel_progress_width
                        if progress_callback is not None:
                            progress_callback(
                                f"加载进度 {start_progress:.1f}%："
                                f"正在读取漏磁 {segment_dir.name}/{path.name}"
                            )
                        if not path.exists():
                            missing.append(str(path))
                            if progress_callback is not None:
                                progress_callback(
                                    f"加载进度 {end_progress:.1f}%："
                                    f"未找到漏磁 {segment_dir.name}/{path.name}"
                                )
                            continue
                        size = path.stat().st_size
                        if size % MFL_RECORD_DTYPE.itemsize != 0:
                            raise ValueError(
                                f"{path} 大小 {size} 不能被记录长度 "
                                f"{MFL_RECORD_DTYPE.itemsize} 整除"
                            )
                        array = np.memmap(
                            path,
                            dtype=MFL_RECORD_DTYPE,
                            mode="r",
                        )
                        channels[key] = array
                        lengths.add(len(array))
                        if progress_callback is not None:
                            progress_callback(
                                f"加载进度 {end_progress:.1f}%："
                                f"已读取漏磁 {segment_dir.name}/{path.name}"
                            )

            if missing:
                raise FileNotFoundError(
                    f"{segment_dir.name} 缺少 {len(missing)} 个漏磁通道，"
                    f"例如：{missing[0]}"
                )
            if len(lengths) != 1:
                raise ValueError(
                    f"{segment_dir.name} 通道记录数不一致：{sorted(lengths)}"
                )

            count = next(iter(lengths))
            if count == 0:
                raise ValueError(f"{segment_dir.name} 通道文件为空")

            if progress_callback is not None:
                progress_callback(
                    f"加载进度 "
                    f"{segment_progress_start + segment_progress_width * map_progress_share:.1f}%："
                    f"正在快速抽查漏磁 {segment_dir.name} 通道索引"
                )

            reference = channels["leftxchannel1"]
            # Probe three short contiguous regions instead of issuing 1,024
            # scattered reads per channel.  The old pattern caused heavy
            # random I/O across every large DAT file on HDD/network storage.
            probe_width = min(32, count)
            probe_starts = sorted(
                {
                    0,
                    max(0, (count - probe_width) // 2),
                    max(0, count - probe_width),
                }
            )
            sample_positions = np.unique(
                np.concatenate(
                    [
                        np.arange(
                            start,
                            start + probe_width,
                            dtype=np.int64,
                        )
                        for start in probe_starts
                    ]
                )
            )
            reference_indices = np.asarray(
                reference["index"][sample_positions]
            )
            for key, array in channels.items():
                indices = np.asarray(array["index"][sample_positions])
                if not np.array_equal(indices, reference_indices):
                    raise ValueError(
                        f"{segment_dir.name} 中 {key} 的 index 与 "
                        "leftxchannel1.dat 不对齐"
                    )

            first_index = int(reference["index"][0])
            last_index = int(reference["index"][count - 1])
            if last_index < first_index:
                raise ValueError(
                    f"{segment_dir.name} 的 index 不是递增方向："
                    f"{first_index} -> {last_index}"
                )

            gap_before_m = float(segment_gaps_m.get(segment_dir.name, 0.0))
            if gap_before_m < 0:
                raise ValueError(
                    f"{segment_dir.name} 的 gap_before_m 不能小于0"
                )

            global_start = (
                next_global_start
                + gap_before_m / self.scale_m_per_unit
            )
            global_end = global_start + (last_index - first_index)
            segment = MFLSegment(
                name=segment_dir.name,
                path=segment_dir,
                channels=channels,
                count=count,
                first_index=first_index,
                last_index=last_index,
                global_start_units=global_start,
                global_end_units=global_end,
                index_monotonic=bool(
                    np.all(np.diff(reference_indices) >= 0)
                ),
                gap_before_m=gap_before_m,
            )
            self.segments.append(segment)
            if not segment.index_monotonic:
                self.warnings.append(
                    f"{segment.name} 的 index 抽样检查不是非递减，"
                    "该段将使用较慢的掩码查询"
                )
            next_global_start = global_end + 1.0
            if stop_callback is not None and stop_callback():
                raise DataLoadCancelled()
            if progress_callback is not None:
                progress_callback(
                    f"加载进度 {segment_progress_end:.1f}%：漏磁 {segment_dir.name} 已快速校验"
                )

        self.segment_by_name = {segment.name: segment for segment in self.segments}
        joint_segment_name = str(joint_config.get("mfl_segment", "EX01"))
        if joint_segment_name not in self.segment_by_name:
            raise ValueError(
                f"接头配置指定的漏磁段不存在：{joint_segment_name}"
            )
        self.joint_segment = self.segment_by_name[joint_segment_name]
        self.joint_record_pos = int(joint_config.get("mfl_record_pos", 0))
        if not 0 <= self.joint_record_pos < self.joint_segment.count:
            raise ValueError(
                f"接头记录序号超出 {joint_segment_name} 范围："
                f"{self.joint_record_pos}"
            )

        actual_raw_index = int(
            self.joint_segment.channels["leftxchannel1"][
                self.joint_record_pos
            ]["index"]
        )
        configured_raw_index = joint_config.get("mfl_raw_index")
        self.joint_raw_index = (
            int(configured_raw_index)
            if configured_raw_index is not None
            else actual_raw_index
        )
        self.joint_global_units = (
            self.joint_segment.global_start_units
            + self.joint_raw_index
            - self.joint_segment.first_index
        )
        if configured_raw_index is not None and int(configured_raw_index) != actual_raw_index:
            self.warnings.append(
                "配置中的 mfl_raw_index 与接头 record_pos 对应的实际 index 不一致；"
                f"配置={configured_raw_index}，实际={actual_raw_index}"
            )
        self.warnings.append(
            "漏磁使用原始 DAT index 作为空间尺度；"
            "旧跨模态脚本中的 record_pos 仅用于定位接头工作锚点"
        )

    @property
    def global_start_units(self) -> float:
        return self.segments[0].global_start_units

    @property
    def global_end_units(self) -> float:
        return self.segments[-1].global_end_units

    @property
    def total_records_per_channel(self) -> int:
        return sum(segment.count for segment in self.segments)

    @property
    def total_scalar_records(self) -> int:
        return self.total_records_per_channel * len(CHANNELS)

    def x_from_global_units(self, global_units: np.ndarray | float) -> np.ndarray | float:
        return (
            np.asarray(global_units) - self.joint_global_units
        ) * self.scale_m_per_unit * self.direction_sign

    def relative_range(self) -> tuple[float, float]:
        values = self.x_from_global_units(
            np.asarray([self.global_start_units, self.global_end_units])
        )
        return float(values.min()), float(values.max())

    def metadata(self) -> dict[str, Any]:
        segments: list[dict[str, Any]] = []
        for segment in self.segments:
            x_start, x_end = self.x_from_global_units(
                np.asarray(
                    [segment.global_start_units, segment.global_end_units]
                )
            )
            segments.append(
                {
                    "name": segment.name,
                    "recordCountPerChannel": segment.count,
                    "channelCount": len(CHANNELS),
                    "firstIndex": segment.first_index,
                    "lastIndex": segment.last_index,
                    "globalStartUnits": segment.global_start_units,
                    "globalEndUnits": segment.global_end_units,
                    "xStartM": float(min(x_start, x_end)),
                    "xEndM": float(max(x_start, x_end)),
                    "gapBeforeM": segment.gap_before_m,
                    "indexMonotonicSampled": segment.index_monotonic,
                    "status": (
                        "warning_tail_zero_possible"
                        if segment.name == "EX01"
                        else "valid"
                    ),
                }
            )

        return {
            "displayRows": len(DISPLAY_ROWS),
            "tracesPerRow": 2,
            "channelCount": len(CHANNELS),
            "recordsPerChannel": self.total_records_per_channel,
            "scalarRecordsAllChannels": self.total_scalar_records,
            "scaleMmPerUnit": self.scale_m_per_unit * 1000.0,
            "segments": segments,
            "joint": {
                "segment": self.joint_segment.name,
                "recordPos": self.joint_record_pos,
                "rawIndex": self.joint_raw_index,
                "globalIndexUnits": self.joint_global_units,
                "xM": 0.0,
            },
        }

    def find_joint(
        self,
        *,
        progress_callback: Callable[[str], None] | None = None,
        stop_callback: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Locate the MFL joint using the global absolute maximum rule."""
        # The application-level locator intentionally uses the direct global
        # absolute maximum.  Keep the older multichannel implementation below
        # for reference and compatibility with previous analysis experiments.
        return self.find_joint_by_absolute_max(
            progress_callback=progress_callback,
            stop_callback=stop_callback,
        )

        if not self.segments:
            raise ValueError("没有可用于接头定位的漏磁段")

        sample_stride = JOINT_SCAN_SAMPLE_STRIDE
        window_records = JOINT_SCAN_WINDOW_RECORDS
        block_records = JOINT_SCAN_BLOCK_RECORDS
        window_samples = max(1, window_records // sample_stride)
        candidates: list[dict[str, Any]] = []
        total_segments = max(1, len(self.segments))

        for segment_number, segment in enumerate(self.segments, start=1):
            if stop_callback is not None and stop_callback():
                raise DataLoadCancelled()
            channels = [segment.channels[name] for name in CHANNELS]
            count = int(segment.count)
            stats_stride = max(512, int(math.ceil(count / 50_000)))
            baseline = np.stack(
                [
                    np.asarray(
                        channel[::stats_stride]["value"],
                        dtype=np.float32,
                    )
                    for channel in channels
                ],
                axis=0,
            )
            medians = np.median(baseline, axis=1).astype(np.float32)
            mad = np.median(
                np.abs(baseline - medians[:, None]),
                axis=1,
            )
            scales = np.maximum(1.4826 * mad, 1.0).astype(np.float32)
            del baseline
            if progress_callback is not None:
                progress_callback(
                    f"接头扫描 {((segment_number - 1) / total_segments) * 80.0 + 5.0:.1f}%："
                    f"正在扫描漏磁 {segment.name} 的多通道响应"
                )

            for block_start in range(0, count, block_records):
                if stop_callback is not None and stop_callback():
                    raise DataLoadCancelled()
                block_stop = min(count, block_start + block_records)
                values = np.stack(
                    [
                        np.asarray(
                            channel[block_start:block_stop:sample_stride]["value"],
                            dtype=np.float32,
                        )
                        for channel in channels
                    ],
                    axis=0,
                )
                complete = (values.shape[1] // window_samples) * window_samples
                if complete == 0:
                    continue
                values = values[:, :complete]
                normalized = np.abs(
                    values - medians[:, None]
                ) / scales[:, None]
                boundary = np.isin(
                    values,
                    np.asarray(JOINT_BOUNDARY_VALUES, dtype=np.float32),
                )
                high = normalized >= JOINT_ROBUST_Z
                grouped_boundary = boundary.reshape(
                    len(CHANNELS), -1, window_samples
                )
                grouped_high = high.reshape(
                    len(CHANNELS), -1, window_samples
                )
                grouped_energy = np.clip(
                    normalized,
                    0.0,
                    32.0,
                ).reshape(len(CHANNELS), -1, window_samples)
                boundary_points = grouped_boundary.sum(axis=(0, 2))
                boundary_channels = grouped_boundary.any(axis=2).sum(axis=0)
                high_points = grouped_high.sum(axis=(0, 2))
                high_channels = grouped_high.any(axis=2).sum(axis=0)
                energy = grouped_energy.mean(axis=(0, 2))
                score = (
                    boundary_points.astype(np.float64) * 12.0
                    + boundary_channels.astype(np.float64) * 24.0
                    + high_points.astype(np.float64) * 0.25
                    + high_channels.astype(np.float64) * 3.0
                    + energy.astype(np.float64)
                )
                top_count = min(4, len(score))
                top_indices = np.argpartition(score, -top_count)[-top_count:]
                for index in top_indices:
                    record_start = block_start + int(index) * window_records
                    record_end = min(count, record_start + window_records)
                    candidates.append(
                        {
                            "segment": segment,
                            "record_start": record_start,
                            "record_end": record_end,
                            "center": (record_start + record_end) // 2,
                            "scan_score": float(score[index]),
                        }
                    )
                if progress_callback is not None:
                    within_segment = block_stop / max(1, count)
                    progress = 5.0 + 80.0 * (
                        (segment_number - 1 + within_segment) / total_segments
                    )
                    progress_callback(
                        f"接头扫描 {progress:.1f}%："
                        f"已扫描漏磁 {segment.name} {block_stop:,}/{count:,} 条记录"
                    )

        if not candidates:
            raise ValueError("没有找到可用于接头定位的候选响应")

        if progress_callback is not None:
            progress_callback("接头扫描 88.0%：正在精细复核候选接头位置")

        # Keep strong candidates from every segment as well as the global
        # strongest responses.  A response in another segment must not hide
        # the configured working segment from ultrasound confirmation.
        ordered_candidates = sorted(
            candidates,
            key=lambda item: float(item["scan_score"]),
            reverse=True,
        )
        refinement_candidates: list[dict[str, Any]] = []
        refinement_keys: set[tuple[str, int, int]] = set()

        def keep_refinement_candidate(candidate: dict[str, Any]) -> None:
            key = (
                str(candidate["segment"].name),
                int(candidate["record_start"]),
                int(candidate["record_end"]),
            )
            if key in refinement_keys:
                return
            refinement_keys.add(key)
            refinement_candidates.append(candidate)

        for segment in self.segments:
            segment_candidates = sorted(
                (
                    candidate
                    for candidate in candidates
                    if candidate["segment"].name == segment.name
                ),
                key=lambda item: float(item["scan_score"]),
                reverse=True,
            )
            for candidate in segment_candidates[:4]:
                keep_refinement_candidate(candidate)
        for candidate in ordered_candidates[:12]:
            keep_refinement_candidate(candidate)

        refined: list[dict[str, Any]] = []
        for candidate in refinement_candidates:
            if stop_callback is not None and stop_callback():
                raise DataLoadCancelled()
            segment = candidate["segment"]
            center = int(candidate["center"])
            refine_start = max(0, center - 2 * window_records)
            refine_stop = min(segment.count, center + 2 * window_records)
            channels = [segment.channels[name] for name in CHANNELS]
            values = np.stack(
                [
                    np.asarray(
                        channel[refine_start:refine_stop]["value"],
                        dtype=np.float32,
                    )
                    for channel in channels
                ],
                axis=0,
            )
            stats_stride = max(512, int(math.ceil(segment.count / 50_000)))
            baseline = np.stack(
                [
                    np.asarray(
                        channel[::stats_stride]["value"],
                        dtype=np.float32,
                    )
                    for channel in channels
                ],
                axis=0,
            )
            medians = np.median(baseline, axis=1).astype(np.float32)
            mad = np.median(
                np.abs(baseline - medians[:, None]),
                axis=1,
            )
            scales = np.maximum(1.4826 * mad, 1.0).astype(np.float32)
            normalized = np.abs(
                values - medians[:, None]
            ) / scales[:, None]
            boundary = np.isin(
                values,
                np.asarray(JOINT_BOUNDARY_VALUES, dtype=np.float32),
            )
            high = normalized >= JOINT_ROBUST_Z
            boundary_per_record = boundary.sum(axis=0).astype(np.float64)
            high_per_record = high.sum(axis=0).astype(np.float64)
            evidence = boundary_per_record * 16.0 + high_per_record
            kernel_width = min(257, len(evidence))
            if kernel_width % 2 == 0:
                kernel_width -= 1
            kernel = np.ones(max(1, kernel_width), dtype=np.float64)
            smoothed = np.convolve(evidence, kernel, mode="same")
            peak_offset = int(np.argmax(smoothed))
            peak_record = refine_start + peak_offset
            boundary_points = int(boundary.sum())
            boundary_channels = int(np.any(boundary, axis=1).sum())
            high_points = int(high.sum())
            high_channels = int(np.any(high, axis=1).sum())
            if boundary_points:
                event_mask = boundary_per_record > 0
                method = "boundary_consensus"
            else:
                threshold = max(
                    4.0,
                    float(np.percentile(high_per_record, 90)),
                )
                event_mask = high_per_record >= threshold
                method = "robust_multichannel_amplitude"
            event_positions = np.flatnonzero(event_mask)
            if len(event_positions):
                near_peak = event_positions[
                    np.abs(event_positions - peak_offset) <= 2 * window_records
                ]
                if len(near_peak):
                    event_start = refine_start + int(near_peak.min())
                    event_end = refine_start + int(near_peak.max())
                else:
                    event_start = max(0, peak_record - window_records // 2)
                    event_end = min(
                        segment.count - 1,
                        peak_record + window_records // 2,
                    )
            else:
                event_start = max(0, peak_record - window_records // 2)
                event_end = min(
                    segment.count - 1,
                    peak_record + window_records // 2,
                )
            reference = segment.channels["leftxchannel1"]
            raw_index = int(reference[peak_record]["index"])
            global_units = (
                segment.global_start_units
                + raw_index
                - segment.first_index
            )
            x_m = float(self.x_from_global_units(global_units))
            confidence = "low"
            if boundary_points >= 20 and boundary_channels >= 12:
                confidence = "high"
            elif (
                boundary_points >= 4 and boundary_channels >= 6
            ) or (high_channels >= 20 and high_points >= 200):
                confidence = "medium"
            priority = (
                boundary_points * 1_000.0
                + boundary_channels * 100.0
                + high_channels * 10.0
                + float(smoothed[peak_offset])
            )
            refined.append(
                {
                    "available": True,
                    "segment": segment.name,
                    "recordPos": peak_record,
                    "rawIndex": raw_index,
                    "xM": x_m,
                    "startRecord": event_start,
                    "endRecord": event_end,
                    "boundaryPoints": boundary_points,
                    "boundaryChannels": boundary_channels,
                    "highPoints": high_points,
                    "highChannels": high_channels,
                    "confidence": confidence,
                    "method": method,
                    "priority": priority,
                }
            )

        if not refined:
            raise ValueError("候选接头无法完成精细定位")
        # Prefer a credible candidate in the configured working segment.  If
        # that segment has no credible response, fall back to the strongest
        # global multichannel response.
        preferred_candidates = [
            item
            for item in refined
            if item["segment"] == self.joint_segment.name
            and item["confidence"] in {"high", "medium"}
        ]
        if preferred_candidates:
            best = max(
                preferred_candidates,
                key=lambda item: float(item["priority"]),
            )
            selection_basis = "configured_segment"
        else:
            best = max(refined, key=lambda item: float(item["priority"]))
            selection_basis = "global_multichannel"

        candidate_payloads = [dict(item) for item in refined]
        best = dict(best)
        best.pop("priority", None)
        best["candidateCount"] = len(refined)
        best["selectionBasis"] = selection_basis
        # ReplayService uses these for optional ultrasound confirmation.
        best["_candidates"] = candidate_payloads
        if progress_callback is not None:
            progress_callback(
                f"接头扫描 100.0%：已定位 {best['segment']} "
                f"record {best['recordPos']:,}（{best['confidence']}）"
            )
        return best

    def find_joint_by_absolute_max(
        self,
        *,
        progress_callback: Callable[[str], None] | None = None,
        stop_callback: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Locate the global MFL sample with the largest absolute value.

        This is intentionally a direct, deterministic rule: every MFL
        channel in every segment is scanned in bounded blocks, and the first
        sample attaining the global maximum of ``abs(value)`` is returned.
        """
        if not self.segments:
            raise ValueError("没有可用于接头定位的漏磁段")

        total_segments = max(1, len(self.segments))
        total_channels = max(1, len(CHANNELS))
        total_units = total_segments * total_channels
        best_abs_value = -1
        best_segment: MFLSegment | None = None
        best_channel = ""
        best_record_pos = 0
        last_reported = -1

        for segment_number, segment in enumerate(self.segments):
            count = int(segment.count)
            for channel_number, channel_name in enumerate(CHANNELS):
                if stop_callback is not None and stop_callback():
                    raise DataLoadCancelled()
                channel = segment.channels[channel_name]
                for block_start in range(0, count, JOINT_SCAN_BLOCK_RECORDS):
                    if stop_callback is not None and stop_callback():
                        raise DataLoadCancelled()
                    block_stop = min(
                        count,
                        block_start + JOINT_SCAN_BLOCK_RECORDS,
                    )
                    values = np.asarray(
                        channel[block_start:block_stop]["value"],
                        dtype=np.int64,
                    )
                    absolute_values = np.abs(values)
                    local_offset = int(np.argmax(absolute_values))
                    local_abs_value = int(absolute_values[local_offset])
                    if local_abs_value > best_abs_value:
                        best_abs_value = local_abs_value
                        best_segment = segment
                        best_channel = channel_name
                        best_record_pos = block_start + local_offset

                    completed_units = (
                        segment_number * total_channels
                        + channel_number
                        + block_stop / max(1, count)
                    )
                    progress = 100.0 * completed_units / total_units
                    progress_marker = int(progress * 2.0)
                    if (
                        progress_callback is not None
                        and progress_marker != last_reported
                    ):
                        last_reported = progress_marker
                        progress_callback(
                            f"\u63a5\u5934\u5b9a\u4f4d {progress:.1f}%\uff1a"
                            f"\u6b63\u5728\u626b\u63cf\u6f0f\u78c1 "
                            f"{segment.name}/{channel_name}"
                            f"\uff0c\u5f53\u524d\u6700\u5927\u7edd\u5bf9\u503c "
                            f"{best_abs_value}"
                        )

        if best_segment is None or best_abs_value < 0:
            raise ValueError("没有找到可用的漏磁绝对值数据")

        reference = best_segment.channels["leftxchannel1"]
        raw_index = int(reference[best_record_pos]["index"])
        global_units = (
            best_segment.global_start_units
            + raw_index
            - best_segment.first_index
        )
        x_m = float(self.x_from_global_units(global_units))
        if progress_callback is not None:
            progress_callback(
                f"\u63a5\u5934\u5b9a\u4f4d 100.0%\uff1a"
                f"\u5df2\u5b9a\u4f4d {best_segment.name} record "
                f"{best_record_pos:,}\uff0c\u7edd\u5bf9\u503c {best_abs_value}"
            )
        return {
            "available": True,
            "segment": best_segment.name,
            "recordPos": best_record_pos,
            "rawIndex": raw_index,
            "xM": x_m,
            "startRecord": best_record_pos,
            "endRecord": best_record_pos,
            "boundaryPoints": 0,
            "boundaryChannels": 0,
            "highPoints": 0,
            "highChannels": 0,
            "absoluteValue": best_abs_value,
            "channel": best_channel,
            "confidence": "direct",
            "method": "absolute_max",
            "selectionBasis": "absolute_max",
            "candidateCount": 1,
        }

    def _record_bounds(
        self,
        segment: MFLSegment,
        global_low: float,
        global_high: float,
    ) -> tuple[int, int]:
        local_low = max(
            0.0,
            global_low - segment.global_start_units,
        )
        local_high = min(
            segment.global_end_units - segment.global_start_units,
            global_high - segment.global_start_units,
        )
        if local_high < 0 or local_low > (
            segment.global_end_units - segment.global_start_units
        ):
            return 0, 0

        raw_low = segment.first_index + local_low
        raw_high = segment.first_index + local_high
        reference = segment.channels["leftxchannel1"]

        if segment.index_monotonic:
            start = int(
                np.searchsorted(reference["index"], raw_low, side="left")
            )
            stop = int(
                np.searchsorted(reference["index"], raw_high, side="right")
            )
            return max(0, start), min(segment.count, stop)

        indices = np.asarray(reference["index"][:])
        positions = np.flatnonzero(
            (indices >= raw_low) & (indices <= raw_high)
        )
        if len(positions) == 0:
            return 0, 0
        return int(positions.min()), int(positions.max()) + 1

    def sample_at(self, x_m: float) -> dict[str, Any]:
        """Read the original MFL sample nearest to one synchronized x position."""
        target_x = float(x_m)
        target_global = (
            self.joint_global_units
            + target_x / (self.scale_m_per_unit * self.direction_sign)
        )
        segment = next(
            (
                item
                for item in self.segments
                if item.global_start_units <= target_global <= item.global_end_units
            ),
            None,
        )
        if segment is None:
            return {"available": False, "x": target_x, "rows": []}

        target_raw_index = (
            segment.first_index
            + target_global
            - segment.global_start_units
        )
        reference_indices = segment.channels["leftxchannel1"]["index"]
        if segment.index_monotonic:
            insertion = int(np.searchsorted(reference_indices, target_raw_index, side="left"))
            candidates = [
                position
                for position in (insertion - 1, insertion)
                if 0 <= position < segment.count
            ]
            record_pos = min(
                candidates,
                key=lambda position: abs(
                    float(reference_indices[position]) - target_raw_index
                ),
            )
        else:
            # Non-monotonic files are not expected for this dataset. Keep the
            # fallback bounded so pointer tracking cannot trigger a full scan.
            span = max(1.0, float(segment.last_index - segment.first_index))
            approximate = int(
                round(
                    (target_raw_index - segment.first_index)
                    / span
                    * max(0, segment.count - 1)
                )
            )
            search_start = max(0, approximate - 1024)
            search_stop = min(segment.count, approximate + 1025)
            local_indices = np.asarray(
                reference_indices[search_start:search_stop],
                dtype=np.float64,
            )
            record_pos = search_start + int(
                np.argmin(np.abs(local_indices - target_raw_index))
            )

        raw_index = float(reference_indices[record_pos])
        global_units = (
            segment.global_start_units + raw_index - segment.first_index
        )
        sample_x = float(self.x_from_global_units(global_units))
        rows: list[dict[str, Any]] = []
        for side, number in DISPLAY_ROWS:
            x_key = f"{side}xchannel{number}"
            z_key = f"{side}zchannel{number}"
            rows.append(
                {
                    "side": side,
                    "channel": number,
                    "xValue": float(segment.channels[x_key]["value"][record_pos]) / 1000.0,
                    "zValue": float(segment.channels[z_key]["value"][record_pos]) / 1000.0,
                }
            )

        return {
            "available": True,
            "requestedX": target_x,
            "x": sample_x,
            "segment": segment.name,
            "recordPos": record_pos,
            "rawIndex": int(reference_indices[record_pos]),
            "rows": rows,
        }

    def locate_range(
        self,
        x_start_m: float,
        x_end_m: float,
    ) -> list[dict[str, Any]]:
        """Map a displayed x interval to exact source DAT record spans.

        The returned record positions are zero-based and inclusive at both
        ends.  A selection crossing EX segment boundaries is returned as
        multiple spans, preserving the original file boundary for later raw
        value reads.
        """
        x_low = min(float(x_start_m), float(x_end_m))
        x_high = max(float(x_start_m), float(x_end_m))
        q_low = x_low / self.direction_sign
        q_high = x_high / self.direction_sign
        q_low, q_high = min(q_low, q_high), max(q_low, q_high)
        global_low = self.joint_global_units + q_low / self.scale_m_per_unit
        global_high = self.joint_global_units + q_high / self.scale_m_per_unit

        spans: list[dict[str, Any]] = []
        for segment in self.segments:
            if (
                segment.global_end_units < global_low
                or segment.global_start_units > global_high
            ):
                continue
            start, stop = self._record_bounds(
                segment,
                global_low,
                global_high,
            )
            if stop <= start:
                continue
            reference = segment.channels["leftxchannel1"]
            start_index = int(reference["index"][start])
            end_index = int(reference["index"][stop - 1])
            start_global = (
                segment.global_start_units
                + start_index
                - segment.first_index
            )
            end_global = (
                segment.global_start_units
                + end_index
                - segment.first_index
            )
            source_x = np.asarray(
                self.x_from_global_units(np.asarray([start_global, end_global]))
            )
            spans.append(
                {
                    "name": segment.name,
                    "recordStart": start,
                    "recordEnd": stop - 1,
                    "sourceIndexStart": start_index,
                    "sourceIndexEnd": end_index,
                    "xStartM": float(source_x.min()),
                    "xEndM": float(source_x.max()),
                    "rawCount": stop - start,
                }
            )
        return spans

    def window(
        self,
        x_start_m: float,
        x_end_m: float,
        max_points: int = DEFAULT_MFL_MAX_POINTS,
    ) -> dict[str, Any]:
        x_low = min(float(x_start_m), float(x_end_m))
        x_high = max(float(x_start_m), float(x_end_m))
        q_low = x_low / self.direction_sign
        q_high = x_high / self.direction_sign
        q_low, q_high = min(q_low, q_high), max(q_low, q_high)
        global_low = self.joint_global_units + q_low / self.scale_m_per_unit
        global_high = self.joint_global_units + q_high / self.scale_m_per_unit

        rows = [
            {
                "side": side,
                "channel": number,
                "label": f"{side.title()} Ch{number}",
                "segments": [],
                "rawCount": 0,
            }
            for side, number in DISPLAY_ROWS
        ]
        source_segments: list[dict[str, Any]] = []
        raw_records_per_channel = 0

        for segment in self.segments:
            if (
                segment.global_end_units < global_low
                or segment.global_start_units > global_high
            ):
                continue

            start, stop = self._record_bounds(
                segment,
                global_low,
                global_high,
            )
            if stop <= start:
                continue

            reference = segment.channels["leftxchannel1"]
            source_index_start = int(reference["index"][start])
            source_index_end = int(reference["index"][stop - 1])
            raw_records_per_channel += stop - start
            source_segments.append(
                {
                    "name": segment.name,
                    "recordStart": start,
                    "recordEnd": stop - 1,
                    "sourceIndexStart": source_index_start,
                    "sourceIndexEnd": source_index_end,
                    "rawCount": stop - start,
                }
            )

            for row_index, (side, number) in enumerate(DISPLAY_ROWS):
                x_key = f"{side}xchannel{number}"
                z_key = f"{side}zchannel{number}"
                sample_x, sample_x_values, sample_z_values = downsample_memmap_pair(
                    reference["index"],
                    segment.channels[x_key]["value"],
                    segment.channels[z_key]["value"],
                    start,
                    stop,
                    segment.global_start_units,
                    segment.first_index,
                    self.joint_global_units,
                    self.scale_m_per_unit,
                    self.direction_sign,
                    None,  # Display every raw record; legacy point limits are ignored.
                )
                rows[row_index]["rawCount"] += stop - start
                rows[row_index]["segments"].append(
                    {
                        "name": segment.name,
                        "x": sample_x.astype(np.float32).tolist(),
                        "xValue": sample_x_values.tolist(),
                        "zValue": sample_z_values.tolist(),
                        "rawCount": stop - start,
                        "recordStart": start,
                        "recordEnd": stop - 1,
                    }
                )

        return {
            "available": True,
            "rawRecordsPerChannel": raw_records_per_channel,
            "sourceSegments": source_segments,
            "rows": rows,
            "maxPointsPerSegment": None,
            "decimated": False,
        }


@dataclass
class USChunk:
    key: int
    path: Path
    records: np.memmap
    id_min: int
    id_max: int
    sorted_by_id_sampled: bool


class UltrasoundDataset:
    """通过现有二进制分片按 ID 查询超声回波。"""

    def __init__(
        self,
        data_dir: Path,
        scale_m_per_unit: float,
        joint_id: int,
        direction_sign: int = 1,
        cache_limit: int = 6,
    ) -> None:
        self.data_dir = data_dir
        self.scale_m_per_unit = float(scale_m_per_unit)
        self.joint_id = int(joint_id)
        self.direction_sign = 1 if int(direction_sign) >= 0 else -1
        self.cache_limit = max(1, int(cache_limit))
        self.cache: OrderedDict[str, np.memmap] = OrderedDict()
        self.manifest = json.loads(
            (data_dir / "manifest.json").read_text(encoding="utf-8-sig")
        )
        self.chunks: list[USChunk] = []
        self.warnings: list[str] = []

        for item in self.manifest.get("chunks", []):
            relative = str(item["file"]).replace("\\", "/").split("/")
            if relative and relative[0] == "data":
                relative = relative[1:]
            path = data_dir.joinpath(*relative)
            if not path.exists():
                self.warnings.append(f"超声分片缺失：{path.name}")
                continue
            records = np.memmap(path, dtype=US_RECORD_DTYPE, mode="r")
            if len(records) == 0:
                continue
            ids = records["id"]
            self.chunks.append(
                USChunk(
                    key=int(item["key"]),
                    path=path,
                    records=records,
                    id_min=int(ids.min()),
                    id_max=int(ids.max()),
                    sorted_by_id_sampled=sample_is_non_decreasing(
                        ids,
                        sample_count=128,
                    ),
                )
            )

        self.chunks.sort(key=lambda item: item.id_min)
        if not self.chunks:
            raise FileNotFoundError(f"未找到超声二进制分片：{data_dir}")

        self.id_min = int(self.manifest.get("idMin", self.chunks[0].id_min))
        self.id_max = int(self.manifest.get("idMax", self.chunks[-1].id_max))
        if len(self.chunks) != len(self.manifest.get("chunks", [])):
            self.warnings.append(
                "部分超声分片不可用，回放时会保留为空白区间"
            )

    def relative_range(self) -> tuple[float, float]:
        values = (
            np.asarray([self.id_min, self.id_max], dtype=np.float64)
            - self.joint_id
        ) * self.scale_m_per_unit * self.direction_sign
        return float(values.min()), float(values.max())

    def _cached_chunk(self, item: USChunk) -> np.memmap:
        cache_key = str(item.path)
        if cache_key in self.cache:
            records = self.cache.pop(cache_key)
            self.cache[cache_key] = records
            return records
        self.cache[cache_key] = item.records
        while len(self.cache) > self.cache_limit:
            self.cache.popitem(last=False)
        return item.records

    def window(
        self,
        x_start_m: float,
        x_end_m: float,
        tracks: list[int] | None = None,
        gates: list[int] | None = None,
        max_records: int = DEFAULT_US_MAX_RECORDS,
    ) -> dict[str, Any]:
        x_low = min(float(x_start_m), float(x_end_m))
        x_high = max(float(x_start_m), float(x_end_m))
        q_low = x_low / self.direction_sign
        q_high = x_high / self.direction_sign
        q_low, q_high = min(q_low, q_high), max(q_low, q_high)
        id_low = int(math.floor(self.joint_id + q_low / self.scale_m_per_unit))
        id_high = int(math.ceil(self.joint_id + q_high / self.scale_m_per_unit)) + 1
        id_low = max(0, id_low)
        id_high = min(np.iinfo(np.uint32).max + 1, id_high)

        blocks: list[np.ndarray] = []
        raw_count = 0
        for chunk in self.chunks:
            if chunk.id_max < id_low or chunk.id_min >= id_high:
                continue

            records = self._cached_chunk(chunk)
            ids = records["id"]
            if chunk.sorted_by_id_sampled:
                start = int(np.searchsorted(ids, id_low, side="left"))
                stop = int(np.searchsorted(ids, id_high, side="left"))
                block = np.asarray(records[start:stop])
            else:
                block = np.asarray(records)
                mask = (block["id"] >= id_low) & (block["id"] < id_high)
                block = block[mask]
            if len(block) == 0:
                continue

            raw_count += len(block)
            mask = np.ones(len(block), dtype=bool)
            if tracks is not None:
                mask &= np.isin(
                    block["track"],
                    np.asarray(tracks, dtype=np.uint8),
                )
            if gates is not None:
                mask &= np.isin(
                    block["gate"],
                    np.asarray(gates, dtype=np.uint8),
                )
            filtered = block[mask]
            if len(filtered):
                blocks.append(filtered)

        if not blocks:
            return {
                "available": True,
                "rawCount": raw_count,
                "count": 0,
                "decimated": False,
                "x": [],
                "depth": [],
                "track": [],
                "gate": [],
                "id": [],
                "segments": [],
            }

        records = (
            blocks[0]
            if len(blocks) == 1
            else np.concatenate(blocks)
        )
        order = np.argsort(records["id"], kind="stable")
        records = records[order]
        original_count = len(records)
        decimated = False
        if original_count > max_records:
            positions = np.linspace(
                0,
                original_count - 1,
                max_records,
                dtype=np.int64,
            )
            records = records[positions]
            decimated = True

        x = (
            records["id"].astype(np.float64) - self.joint_id
        ) * self.scale_m_per_unit * self.direction_sign
        depth = records["depth"].astype(np.float64) * 176.0 / 127.0
        return {
            "available": True,
            "rawCount": raw_count,
            "count": len(records),
            "originalCountAfterFilter": original_count,
            "decimated": decimated,
            "x": x.astype(np.float32).tolist(),
            "depth": depth.astype(np.float32).tolist(),
            "track": records["track"].astype(np.uint8).tolist(),
            "gate": records["gate"].astype(np.uint8).tolist(),
            "id": records["id"].astype(np.uint32).tolist(),
            "segments": sorted(
                {int(value) for value in records["segment"]}
            ),
        }

    def assess_joint_candidate(
        self,
        x_m: float,
        *,
        half_width_m: float = 1.5,
    ) -> dict[str, Any]:
        """Check whether a candidate has the expected local US joint pattern."""
        payload = self.window(
            float(x_m) - float(half_width_m),
            float(x_m) + float(half_width_m),
            tracks=[0, 1],
            gates=None,
            max_records=200_000,
        )
        gates = {int(value) for value in payload.get("gate", [])}
        tracks = {int(value) for value in payload.get("track", [])}
        expected_hit = gates.intersection(JOINT_US_GATES)
        gate_coverage = len(expected_hit) / max(1, len(JOINT_US_GATES))
        return {
            "available": True,
            "predictedId": int(
                round(
                    self.joint_id
                    + float(x_m)
                    / (self.scale_m_per_unit * self.direction_sign)
                )
            ),
            "echoCount": int(payload.get("rawCount", 0)),
            "gates": sorted(gates),
            "tracks": sorted(tracks),
            "expectedGateHit": sorted(expected_hit),
            "gateCoverage": float(gate_coverage),
            "twoTrack": tracks == {0, 1},
            "confidence": (
                "high"
                if gate_coverage >= 0.65 and tracks == {0, 1}
                else "medium"
                if gate_coverage >= 0.35 or tracks == {0, 1}
                else "low"
            ),
        }

    def find_joint_candidate(
        self,
        *,
        half_width_m: float = 0.5,
        progress_callback: Callable[[str], None] | None = None,
        stop_callback: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Find a likely US joint from its multi-gate, two-track echo pattern.

        The scan ranks 1 m neighborhoods by expected GateNo coverage, presence
        on both tracks, and then the number of expected-gate echoes.  IDs are
        binned only for the search; the reported ID is the echo-weighted median
        within the best neighborhood.
        """
        if half_width_m <= 0:
            raise ValueError("超声接头搜索窗口必须大于0")

        expected_gates = tuple(sorted(JOINT_US_GATES))
        gate_count = len(expected_gates)
        pair_feature_count = gate_count * 2
        track_feature_offset = pair_feature_count
        bin_width = 32
        bin_size_m = bin_width * self.scale_m_per_unit
        radius_bins = max(1, int(math.ceil(half_width_m / bin_size_m)))
        gate_lookup = np.full(256, 255, dtype=np.uint8)
        for gate_index, gate in enumerate(expected_gates):
            gate_lookup[gate] = np.uint8(gate_index)

        bin_masks: dict[int, int] = {}
        bin_echo_counts: dict[int, int] = {}
        total_records = sum(len(chunk.records) for chunk in self.chunks)
        if total_records <= 0:
            raise ValueError("超声数据中没有可扫描的回波记录")

        processed_records = 0
        block_size = 1_000_000
        for chunk_index, chunk in enumerate(self.chunks, start=1):
            for start in range(0, len(chunk.records), block_size):
                if stop_callback is not None and stop_callback():
                    raise DataLoadCancelled()
                records = chunk.records[start : start + block_size]
                ids = np.asarray(records["id"], dtype=np.uint64)
                tracks = np.asarray(records["track"], dtype=np.uint8)
                gates = np.asarray(records["gate"], dtype=np.uint8)
                buckets = ids // np.uint64(bin_width)

                gate_indices = gate_lookup[gates]
                valid_tracks = tracks < 2
                expected_echoes = (gate_indices < gate_count) & valid_tracks
                row_masks = np.zeros(len(records), dtype=np.uint32)
                row_masks[expected_echoes] = np.uint32(1) << (
                    gate_indices[expected_echoes].astype(np.uint32) * 2
                    + tracks[expected_echoes]
                )
                row_masks[valid_tracks] |= (
                    np.uint32(1) << (track_feature_offset + tracks[valid_tracks])
                )
                unique_buckets, inverse = np.unique(
                    buckets,
                    return_inverse=True,
                )
                grouped_masks = np.zeros(len(unique_buckets), dtype=np.uint32)
                np.bitwise_or.at(grouped_masks, inverse, row_masks)
                grouped_echo_counts = np.bincount(
                    inverse,
                    weights=expected_echoes.astype(np.uint8),
                    minlength=len(unique_buckets),
                )

                for bucket, mask, echo_count in zip(
                    unique_buckets,
                    grouped_masks,
                    grouped_echo_counts,
                ):
                    bucket_key = int(bucket)
                    bin_masks[bucket_key] = (
                        bin_masks.get(bucket_key, 0) | int(mask)
                    )
                    bin_echo_counts[bucket_key] = (
                        bin_echo_counts.get(bucket_key, 0) + int(echo_count)
                    )

                processed_records += len(records)
                if progress_callback is not None:
                    percent = 5.0 + 88.0 * processed_records / total_records
                    progress_callback(
                        f"接头定位 {percent:.1f}%：扫描超声回波"
                        f"（分片 {chunk_index}/{len(self.chunks)}）"
                    )

        if stop_callback is not None and stop_callback():
            raise DataLoadCancelled()
        if not bin_masks:
            raise ValueError("超声数据中没有可用于接头检测的记录")

        buckets = np.asarray(sorted(bin_masks), dtype=np.int64)
        masks = np.fromiter(
            (bin_masks[int(bucket)] for bucket in buckets),
            dtype=np.uint32,
            count=len(buckets),
        )
        echo_counts = np.fromiter(
            (bin_echo_counts[int(bucket)] for bucket in buckets),
            dtype=np.int64,
            count=len(buckets),
        )
        feature_count = pair_feature_count + 2
        feature_totals = np.zeros(feature_count, dtype=np.int64)
        feature_bits = tuple(1 << bit for bit in range(feature_count))
        left = 0
        right = 0
        window_echo_count = 0
        best_score = (-1, -1, -1, -1)
        best_bounds = (int(buckets[0]), int(buckets[0]))

        for index, center_bucket_value in enumerate(buckets):
            if index % 4096 == 0:
                if stop_callback is not None and stop_callback():
                    raise DataLoadCancelled()
                if progress_callback is not None:
                    percent = 93.0 + 6.0 * index / max(1, len(buckets))
                    progress_callback(
                        f"接头定位 {percent:.1f}%：比较超声多Gate双Track特征"
                    )

            center_bucket = int(center_bucket_value)
            lower_bucket = center_bucket - radius_bins
            upper_bucket = center_bucket + radius_bins
            while right < len(buckets) and int(buckets[right]) <= upper_bucket:
                mask = int(masks[right])
                for bit, feature_bit in enumerate(feature_bits):
                    if mask & feature_bit:
                        feature_totals[bit] += 1
                window_echo_count += int(echo_counts[right])
                right += 1
            while left < right and int(buckets[left]) < lower_bucket:
                mask = int(masks[left])
                for bit, feature_bit in enumerate(feature_bits):
                    if mask & feature_bit:
                        feature_totals[bit] -= 1
                window_echo_count -= int(echo_counts[left])
                left += 1

            detected_gates = [
                gate
                for gate_index, gate in enumerate(expected_gates)
                if feature_totals[gate_index * 2] > 0
                or feature_totals[gate_index * 2 + 1] > 0
            ]
            detected_tracks = [
                track
                for track in (0, 1)
                if feature_totals[track_feature_offset + track] > 0
            ]
            detected_pairs = sum(
                int(feature_totals[gate_index * 2] > 0)
                + int(feature_totals[gate_index * 2 + 1] > 0)
                for gate_index in range(gate_count)
            )
            score = (
                len(detected_gates),
                int(detected_tracks == [0, 1]),
                detected_pairs,
                window_echo_count,
            )
            if score > best_score:
                best_score = score
                best_bounds = (lower_bucket, upper_bucket)

        best_left = int(np.searchsorted(buckets, best_bounds[0], side="left"))
        best_right = int(np.searchsorted(buckets, best_bounds[1], side="right"))
        candidate_buckets = buckets[best_left:best_right]
        candidate_weights = echo_counts[best_left:best_right]
        active = candidate_weights > 0
        candidate_buckets = candidate_buckets[active]
        candidate_weights = candidate_weights[active]
        if not len(candidate_buckets):
            raise ValueError(
                "没有发现包含预期超声 Gate 的候选区间；请检查 Gate 配置或数据格式"
            )

        weight_midpoint = (int(candidate_weights.sum()) - 1) // 2
        median_index = int(
            np.searchsorted(np.cumsum(candidate_weights), weight_midpoint + 1)
        )
        candidate_id = min(
            self.id_max,
            max(
                self.id_min,
                int(candidate_buckets[median_index]) * bin_width + bin_width // 2,
            ),
        )
        gate_coverage = best_score[0] / max(1, gate_count)
        pair_coverage = best_score[2] / max(1, pair_feature_count)
        # Recompute the winning-window feature summary; the rolling counters
        # currently correspond to the final window after the sweep.
        winner_mask_counts = np.zeros(feature_count, dtype=np.int64)
        winner_echo_count = 0
        for bucket_index in range(best_left, best_right):
            if best_bounds[0] <= int(buckets[bucket_index]) <= best_bounds[1]:
                mask = int(masks[bucket_index])
                for bit, feature_bit in enumerate(feature_bits):
                    if mask & feature_bit:
                        winner_mask_counts[bit] += 1
                winner_echo_count += int(echo_counts[bucket_index])
        detected_tracks = [
            track
            for track in (0, 1)
            if winner_mask_counts[track_feature_offset + track] > 0
        ]
        detected_gates = [
            gate
            for gate_index, gate in enumerate(expected_gates)
            if winner_mask_counts[gate_index * 2] > 0
            or winner_mask_counts[gate_index * 2 + 1] > 0
        ]
        confidence = (
            "high"
            if gate_coverage >= 0.65 and detected_tracks == [0, 1]
            else "medium"
            if gate_coverage >= 0.35 or detected_tracks == [0, 1]
            else "low"
        )
        relative_offset_m = (
            (candidate_id - self.joint_id)
            * self.scale_m_per_unit
            * self.direction_sign
        )
        if progress_callback is not None:
            progress_callback("接头定位 100.0%：超声接头候选已找到")
        return {
            "available": True,
            "method": "ultrasound_gate_track_coverage",
            "candidateId": candidate_id,
            "currentJointId": self.joint_id,
            "offsetFromCurrentAnchorM": float(relative_offset_m),
            "searchWindowM": float(2.0 * half_width_m),
            "echoCount": int(winner_echo_count),
            "gates": detected_gates,
            "expectedGates": list(expected_gates),
            "gateCoverage": float(gate_coverage),
            "gateTrackCoverage": float(pair_coverage),
            "tracks": detected_tracks,
            "confidence": confidence,
        }

    def metadata(self) -> dict[str, Any]:
        gate_counts = {
            int(key): int(value)
            for key, value in self.manifest.get("gateCounts", {}).items()
            if int(value) > 0
        }
        name_by_gate = {
            int(item["gate"]): str(item.get("name", ""))
            for item in self.manifest.get("gates", [])
        }
        gates = [
            {
                "gate": gate,
                "count": count,
                "name": name_by_gate.get(gate, f"Gate {gate}"),
            }
            for gate, count in sorted(gate_counts.items())
        ]
        track_counts = {
            str(key): int(value)
            for key, value in self.manifest.get("trackCounts", {}).items()
        }
        x_start, x_end = self.relative_range()
        return {
            "available": True,
            "totalRecords": int(self.manifest.get("totalRecords", 0)),
            "idMin": self.id_min,
            "idMax": self.id_max,
            "xStartM": x_start,
            "xEndM": x_end,
            "virtualMileage": {
                "min": self.manifest.get("mileageMin"),
                "max": self.manifest.get("mileageMax"),
            },
            "chunkCount": len(self.chunks),
            "trackCounts": track_counts,
            "gates": gates,
            "warnings": list(self.warnings),
        }


class ReplayService:
    def __init__(
        self,
        config_path: Path,
        progress_callback: Callable[[str], None] | None = None,
        stop_callback: Callable[[], bool] | None = None,
    ) -> None:
        self.config_path = config_path
        self.config = json.loads(
            config_path.read_text(encoding="utf-8-sig")
        )
        self.dataset_name = str(self.config.get("dataset", "20251221"))
        self.scale_mm_per_unit = float(
            self.config.get("scale_mm_per_unit", DEFAULT_SCALE_MM)
        )
        if self.scale_mm_per_unit <= 0:
            raise ValueError("scale_mm_per_unit 必须大于0")
        self.scale_m_per_unit = self.scale_mm_per_unit / 1000.0
        self.direction_sign = (
            1 if int(self.config.get("direction_sign", 1)) >= 0 else -1
        )

        mfl_path = resolve_path(
            self.config.get(
                "mfl_data_dir", "portable-release/data/20251221/漏磁"
            ),
            config_path.parent,
        )
        us_path_value = self.config.get(
            "ultrasound_data_dir",
            "portable-release/data/20251221/超声/data",
        )
        us_path = resolve_path(us_path_value, config_path.parent)
        us_csv_path_value = self.config.get("ultrasound_csv_dir")
        us_csv_path = (
            resolve_path(us_csv_path_value, config_path.parent)
            if us_csv_path_value
            else None
        )
        def report_ultrasound_progress(message: str) -> None:
            if progress_callback is None:
                return
            match = re.search(r"(\d+(?:\.\d+)?)\s*%", message)
            if match:
                local_progress = max(0.0, min(100.0, float(match.group(1))))
                progress = 5.0 + local_progress * 0.15
            elif "完成" in message:
                progress = 20.0
            else:
                progress = 10.0
            progress_callback(f"加载进度 {progress:.1f}%：{message}")

        if progress_callback is not None:
            progress_callback("加载进度 5.0%：正在检查超声二进制缓存")
        us_path = ensure_binary_data(
            us_path,
            us_csv_path,
            dataset=self.dataset_name,
            progress_callback=report_ultrasound_progress,
            stop_callback=stop_callback,
        )
        if progress_callback is not None:
            progress_callback("加载进度 20.0%：开始读取漏磁数据")
        joint_config = dict(self.config.get("joint", {}))
        gaps = {
            str(key): float(value)
            for key, value in self.config.get(
                "mfl_segment_gaps_m",
                {},
            ).items()
        }

        self.mfl = MFLDataset(
            data_dir=mfl_path,
            scale_m_per_unit=self.scale_m_per_unit,
            segment_gaps_m=gaps,
            joint_config=joint_config,
            direction_sign=self.direction_sign,
            stop_callback=stop_callback,
            progress_callback=progress_callback,
        )
        if progress_callback is not None:
            progress_callback("加载进度 88.0%：正在建立超声索引")
        self.us: UltrasoundDataset | None = None
        self.us_error: str | None = None
        us_joint_id = int(joint_config.get("us_id", 2794006))
        try:
            self.us = UltrasoundDataset(
                data_dir=us_path,
                scale_m_per_unit=self.scale_m_per_unit,
                joint_id=us_joint_id,
                direction_sign=self.direction_sign,
            )
        except Exception as exc:
            self.us_error = str(exc)
            self.mfl.warnings.append(
                "超声数据未能加载；请检查 ultrasound_data_dir 和 manifest.json"
            )
            if progress_callback is not None:
                progress_callback(
                    f"加载进度 95.0%：未找到数据集 {self.dataset_name} 的超声数据，"
                    "当前仅加载漏磁"
                )
        else:
            if progress_callback is not None:
                progress_callback("加载进度 95.0%：超声数据索引已建立")

        ranges = [self.mfl.relative_range()]
        if self.us is not None:
            ranges.append(self.us.relative_range())
        self.range_start_m = min(item[0] for item in ranges)
        self.range_end_m = max(item[1] for item in ranges)
        self.joint_config = joint_config
        self.us_joint_id = us_joint_id
        if progress_callback is not None:
            progress_callback("加载进度 100.0%：数据加载完成")

    def locate_mfl_range(
        self,
        x_start_m: float,
        x_end_m: float,
    ) -> list[dict[str, Any]]:
        """Return exact MFL source spans for an annotation interval."""
        return self.mfl.locate_range(x_start_m, x_end_m)

    def locate_joint(
        self,
        *,
        progress_callback: Callable[[str], None] | None = None,
        stop_callback: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Locate a joint from MFL and optionally cross-check ultrasound."""
        result = self.mfl.find_joint(
            progress_callback=progress_callback,
            stop_callback=stop_callback,
        )
        result.pop("_candidates", None)
        result["selectionBasis"] = "absolute_max"
        if self.us is None:
            result["ultrasound"] = {
                "available": False,
                "confidence": "unavailable",
                "usedForSelection": False,
            }
        else:
            result["ultrasound"] = {
                "available": True,
                "confidence": "not_used",
                "usedForSelection": False,
            }
        return result

    def locate_ultrasound_joint(
        self,
        *,
        progress_callback: Callable[[str], None] | None = None,
        stop_callback: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Search ultrasound echoes for the expected multi-gate joint pattern."""
        if self.us is None:
            raise ValueError(self.us_error or "当前数据集没有可用的超声数据")
        return self.us.find_joint_candidate(
            progress_callback=progress_callback,
            stop_callback=stop_callback,
        )

    def manifest(self) -> dict[str, Any]:
        mfl_meta = self.mfl.metadata()
        warnings = list(self.mfl.warnings)
        if self.us is not None:
            us_meta = self.us.metadata()
            warnings.extend(self.us.warnings)
        else:
            us_meta = {
                "available": False,
                "error": self.us_error,
                "totalRecords": 0,
                "gates": [],
                "warnings": [self.us_error] if self.us_error else [],
            }

        return {
            "ok": True,
            "dataset": self.dataset_name,
            "coordinate": {
                "type": "joint_relative_distance",
                "unit": "m",
                "scaleMmPerUnit": self.scale_mm_per_unit,
                "directionSign": self.direction_sign,
                "origin": "confirmed_joint_center",
                "primaryFieldMfl": "DAT.index",
                "primaryFieldUltrasound": "ID",
                "ignoredFieldUltrasound": "Mileage",
            },
            "joint": {
                "status": self.joint_config.get("status", "working_anchor"),
                "mflSegment": self.mfl.joint_segment.name,
                "mflRecordPos": self.mfl.joint_record_pos,
                "mflRawIndex": self.mfl.joint_raw_index,
                "mflGlobalIndexUnits": self.mfl.joint_global_units,
                "usId": self.us_joint_id,
                "xM": 0.0,
            },
            "range": {
                "startM": self.range_start_m,
                "endM": self.range_end_m,
                "mfl": {
                    "startM": self.mfl.relative_range()[0],
                    "endM": self.mfl.relative_range()[1],
                },
                "ultrasound": (
                    {
                        "startM": self.us.relative_range()[0],
                        "endM": self.us.relative_range()[1],
                    }
                    if self.us is not None
                    else None
                ),
            },
            "mfl": mfl_meta,
            "ultrasound": us_meta,
            "warnings": warnings,
        }

    def window(
        self,
        x_start_m: float,
        x_end_m: float,
        tracks: list[int] | None = None,
        gates: list[int] | None = None,
        max_mfl_points: int = DEFAULT_MFL_MAX_POINTS,
        max_us_records: int = DEFAULT_US_MAX_RECORDS,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        x_start = max(self.range_start_m, min(float(x_start_m), self.range_end_m))
        x_end = max(self.range_start_m, min(float(x_end_m), self.range_end_m))
        if x_end < x_start:
            x_start, x_end = x_end, x_start
        if x_end == x_start:
            x_end = min(self.range_end_m, x_start + 0.001)

        mfl = self.mfl.window(
            x_start,
            x_end,
            max_points=max_mfl_points,
        )
        if self.us is not None:
            ultrasound = self.us.window(
                x_start,
                x_end,
                tracks=tracks,
                gates=gates,
                max_records=max_us_records,
            )
        else:
            ultrasound = {
                "available": False,
                "error": self.us_error,
                "rawCount": 0,
                "count": 0,
                "x": [],
                "depth": [],
                "track": [],
                "gate": [],
                "id": [],
                "segments": [],
            }

        return {
            "ok": True,
            "xStartM": x_start,
            "xEndM": x_end,
            "mfl": mfl,
            "ultrasound": ultrasound,
            "filters": {
                "tracks": tracks,
                "gates": gates,
            },
            "serverMs": round((time.perf_counter() - started) * 1000.0, 2),
        }


def parse_int_list(params: dict[str, list[str]], key: str) -> list[int] | None:
    values = params.get(key)
    if not values or not values[0] or values[0].lower() == "all":
        return None
    if values[0].lower() == "none":
        return []
    result: list[int] = []
    for token in values[0].split(","):
        token = token.strip()
        if token:
            result.append(int(token))
    return sorted(set(result)) or None


class ReplayHandler(BaseHTTPRequestHandler):
    server_version = "SynchronizedReplay/1.0"

    def log_message(self, format_string: str, *args: Any) -> None:
        print("[http] " + format_string % args)

    @property
    def service(self) -> ReplayService:
        return self.server.replay_service  # type: ignore[attr-defined]

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json(
            {
                "ok": False,
                "error": message,
            },
            status=status,
        )

    def do_GET(self) -> None:
        parsed = self.path.split("?", 1)
        route = parsed[0]
        query_string = parsed[1] if len(parsed) == 2 else ""
        from urllib.parse import parse_qs, unquote

        if route == "/api/manifest":
            try:
                self.send_json(self.service.manifest())
            except Exception as exc:
                traceback.print_exc()
                self.send_error_json(str(exc), status=500)
            return

        if route == "/api/window":
            try:
                params = parse_qs(query_string)
                x_start = float(params.get("x_start", [self.service.range_start_m])[0])
                x_end = float(
                    params.get(
                        "x_end",
                        [x_start + float(self.service.config.get(
                            "render",
                            {},
                        ).get("default_window_m", 8.0))],
                    )[0]
                )
                if not math.isfinite(x_start) or not math.isfinite(x_end):
                    raise ValueError("x_start/x_end 必须是有限数字")
                if abs(x_end - x_start) > MAX_DETAIL_WINDOW_M:
                    raise ValueError(
                        f"细节窗口不能超过 {MAX_DETAIL_WINDOW_M:.0f} m"
                    )
                tracks = parse_int_list(params, "tracks")
                gates = parse_int_list(params, "gates")
                if tracks is not None and any(track not in (0, 1) for track in tracks):
                    raise ValueError("tracks 只能包含0或1")
                if gates is not None and any(gate < 0 or gate > 255 for gate in gates):
                    raise ValueError("gates 必须位于0～255")
                render_config = self.service.config.get("render", {})
                max_mfl_points = int(
                    params.get(
                        "max_mfl_points",
                        [render_config.get(
                            "max_mfl_points_per_segment",
                            DEFAULT_MFL_MAX_POINTS,
                        )],
                    )[0]
                )
                max_us_records = int(
                    params.get(
                        "max_us_records",
                        [render_config.get(
                            "max_ultrasound_records",
                            DEFAULT_US_MAX_RECORDS,
                        )],
                    )[0]
                )
                max_mfl_points = min(max(32, max_mfl_points), 20_000)
                max_us_records = min(max(1000, max_us_records), 500_000)
                self.send_json(
                    self.service.window(
                        x_start,
                        x_end,
                        tracks=tracks,
                        gates=gates,
                        max_mfl_points=max_mfl_points,
                        max_us_records=max_us_records,
                    )
                )
            except ValueError as exc:
                self.send_error_json(str(exc), status=400)
            except Exception as exc:
                traceback.print_exc()
                self.send_error_json(str(exc), status=500)
            return

        relative = unquote(route.lstrip("/"))
        if not relative:
            relative = "index.html"
        web_root = (APP_DIR / "web").resolve()
        target = (web_root / relative).resolve()
        try:
            target.relative_to(web_root)
        except ValueError:
            self.send_error(403, "Forbidden")
            return
        if not target.is_file():
            self.send_error(404, "Not Found")
            return

        content = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="20251221 超声—漏磁同步数据回放程序"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=APP_DIR / "config.json",
        help="回放配置文件",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument(
        "--open",
        action="store_true",
        help="启动服务后自动打开浏览器",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = resolve_path(args.config, APP_DIR)
    service = ReplayService(config_path)
    server = ThreadingHTTPServer((args.host, args.port), ReplayHandler)
    server.daemon_threads = True
    server.replay_service = service  # type: ignore[attr-defined]

    url = f"http://{args.host}:{args.port}/"
    print(f"同步回放服务已启动：{url}")
    print(f"配置文件：{config_path}")
    print(
        f"统一横轴范围：{service.range_start_m:.3f} m"
        f" ～ {service.range_end_m:.3f} m"
    )
    print(
        f"漏磁：{service.mfl.total_records_per_channel:,} 条/通道，"
        f"{len(CHANNELS)} 通道"
    )
    if service.us is not None:
        print(
            f"超声：{service.us.manifest.get('totalRecords', 0):,} 条回波，"
            f"{len(service.us.chunks)} 个分片"
        )
    else:
        print(f"超声未加载：{service.us_error}")

    if args.open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭同步回放服务")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
