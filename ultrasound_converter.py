"""Convert the raw ultrasound CSV files into the replay binary cache.

The desktop replay reads the compact binary cache because the raw CSV files
contain millions of rows.  This module keeps the conversion inside the
desktop application so a user only needs to provide the original CSV folder.
The output layout and record dtype intentionally match the existing
``build_player_data.py`` converter used by the ultrasound project.
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Callable

import numpy as np


COMBINED_FILE_RE = re.compile(r"_S(\d+)\.csv$", re.IGNORECASE)
REQUIRED_COLUMNS = ("Track", "ID", "GateNo", "Depth", "Mileage")
RECORD_DTYPE = np.dtype(
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

ProgressCallback = Callable[[str], None]
StopCallback = Callable[[], bool]


class ConversionCancelled(RuntimeError):
    """Raised when the user closes the application during conversion."""


def combined_csv_files(input_dir: Path) -> list[tuple[Path, int]]:
    """Return the main ``*_S0.csv`` ... ``*_Sn.csv`` files.

    The raw directory also contains files such as ``*_S0_0.csv`` and
    ``*_S0_1.csv``.  The original converter deliberately processes only the
    combined files ending in ``_S<number>.csv``; keeping that rule prevents
    double-counting the split copies.
    """

    result: list[tuple[Path, int]] = []
    if not input_dir.exists():
        return result
    for path in input_dir.glob("*.csv"):
        match = COMBINED_FILE_RE.search(path.name)
        if match:
            result.append((path, int(match.group(1))))
    result.sort(key=lambda item: item[1])
    return result


def has_binary_cache(data_dir: Path) -> bool:
    return (data_dir / "manifest.json").exists() and any(
        data_dir.glob("chunk_*.bin")
    )


def _detect_encoding(path: Path) -> str:
    sample = path.read_bytes()[:4 * 1024 * 1024]
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError(
        "unknown",
        sample,
        0,
        len(sample),
        f"无法识别 CSV 编码：{path.name}",
    )


def _parse_integer(value: str, *, path: Path, row_number: int, field: str) -> int:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{path.name}:{row_number} 的 {field} 不是数字：{value!r}"
        ) from exc
    if not math.isfinite(number) or number != int(number):
        raise ValueError(
            f"{path.name}:{row_number} 的 {field} 不是整数：{value!r}"
        )
    return int(number)


def _parse_float(value: str, *, path: Path, row_number: int, field: str) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{path.name}:{row_number} 的 {field} 不是数字：{value!r}"
        ) from exc
    if not math.isfinite(number):
        raise ValueError(
            f"{path.name}:{row_number} 的 {field} 不是有限数：{value!r}"
        )
    return number


def _report(callback: ProgressCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _check_stop(callback: StopCallback | None) -> None:
    if callback is not None and callback():
        raise ConversionCancelled("超声 CSV 转换已取消")


def convert_csv_to_binary(
    input_dir: Path,
    output_dir: Path,
    *,
    dataset: str = "20251221",
    chunk_width: float = 1.0,
    batch_size: int = 100_000,
    force: bool = False,
    progress_callback: ProgressCallback | None = None,
    stop_callback: StopCallback | None = None,
) -> dict:
    """Convert raw CSV files into the binary cache consumed by the player."""

    input_dir = Path(input_dir).resolve()
    output_dir = Path(output_dir).resolve()
    if chunk_width <= 0:
        raise ValueError("超声二进制分片宽度必须大于 0")
    if batch_size < 1:
        raise ValueError("CSV 转换批大小必须大于 0")

    files = combined_csv_files(input_dir)
    if not files:
        raise FileNotFoundError(
            f"未找到主超声 CSV：{input_dir} 中需要 *_S0.csv、*_S1.csv 等文件"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    existing = list(output_dir.glob("chunk_*.bin"))
    manifest_path = output_dir / "manifest.json"
    if existing and not force:
        raise FileExistsError(
            f"{output_dir} 已存在超声二进制缓存；如需重建请使用 force"
        )
    if force:
        for path in existing:
            path.unlink()
        if manifest_path.exists():
            manifest_path.unlink()

    total_bytes = sum(path.stat().st_size for path, _segment in files)
    processed_bytes = 0
    chunk_counts: Counter[int] = Counter()
    track_counts: Counter[int] = Counter()
    gate_counts: Counter[int] = Counter()
    total_rows = 0
    mileage_min = math.inf
    mileage_max = -math.inf
    id_min = math.inf
    id_max = -math.inf
    depth_min = math.inf
    depth_max = -math.inf

    for path, segment in files:
        _check_stop(stop_callback)
        encoding = _detect_encoding(path)
        _report(progress_callback, f"正在读取超声 CSV：{path.name}（{encoding}）")
        mileage_batch: list[float] = []
        id_batch: list[int] = []
        depth_batch: list[int] = []
        gate_batch: list[int] = []
        track_batch: list[int] = []

        def flush_batch() -> None:
            nonlocal mileage_min, mileage_max, id_min, id_max
            nonlocal depth_min, depth_max, total_rows
            if not mileage_batch:
                return
            records = np.empty(len(mileage_batch), dtype=RECORD_DTYPE)
            records["mileage"] = np.asarray(mileage_batch, dtype=np.float32)
            records["id"] = np.asarray(id_batch, dtype=np.uint32)
            records["depth"] = np.asarray(depth_batch, dtype=np.uint8)
            records["gate"] = np.asarray(gate_batch, dtype=np.uint8)
            records["track"] = np.asarray(track_batch, dtype=np.uint8)
            records["segment"] = segment

            keys = np.floor(records["mileage"] / chunk_width).astype(np.int32)
            for key in np.unique(keys):
                selected = records[keys == key]
                output_path = output_dir / f"chunk_{int(key):05d}.bin"
                with output_path.open("ab") as stream:
                    stream.write(selected.tobytes(order="C"))
                chunk_counts[int(key)] += len(selected)

            count = len(records)
            total_rows += count
            mileage_min = min(mileage_min, float(records["mileage"].min()))
            mileage_max = max(mileage_max, float(records["mileage"].max()))
            id_min = min(id_min, int(records["id"].min()))
            id_max = max(id_max, int(records["id"].max()))
            depth_min = min(depth_min, int(records["depth"].min()))
            depth_max = max(depth_max, int(records["depth"].max()))
            track_counts.update(int(value) for value in records["track"])
            gate_counts.update(int(value) for value in records["gate"])
            mileage_batch.clear()
            id_batch.clear()
            depth_batch.clear()
            gate_batch.clear()
            track_batch.clear()

        with path.open("r", encoding=encoding, newline="") as stream:
            reader = csv.reader(stream)
            header = next(reader, None)
            if header is None:
                raise ValueError(f"CSV 为空：{path.name}")
            normalized_header = [
                str(name).strip().lstrip("\ufeff") for name in header
            ]
            missing = [
                name for name in REQUIRED_COLUMNS if name not in normalized_header
            ]
            if missing:
                raise ValueError(f"{path.name} 缺少字段：{missing}")
            indexes = {name: normalized_header.index(name) for name in REQUIRED_COLUMNS}
            max_index = max(indexes.values())

            for row_number, row in enumerate(reader, start=2):
                _check_stop(stop_callback)
                if not row or not any(str(value).strip() for value in row):
                    continue
                if len(row) <= max_index:
                    raise ValueError(f"{path.name}:{row_number} 字段数量不足")
                track = _parse_integer(
                    row[indexes["Track"]],
                    path=path,
                    row_number=row_number,
                    field="Track",
                )
                ident = _parse_integer(
                    row[indexes["ID"]],
                    path=path,
                    row_number=row_number,
                    field="ID",
                )
                gate = _parse_integer(
                    row[indexes["GateNo"]],
                    path=path,
                    row_number=row_number,
                    field="GateNo",
                )
                depth = _parse_integer(
                    row[indexes["Depth"]],
                    path=path,
                    row_number=row_number,
                    field="Depth",
                )
                mileage = _parse_float(
                    row[indexes["Mileage"]],
                    path=path,
                    row_number=row_number,
                    field="Mileage",
                )
                if track not in (0, 1):
                    raise ValueError(f"{path.name}:{row_number} Track 超出 0/1")
                if not 0 <= ident <= np.iinfo(np.uint32).max:
                    raise ValueError(f"{path.name}:{row_number} ID 超出 uint32")
                if not 0 <= gate <= np.iinfo(np.uint8).max:
                    raise ValueError(f"{path.name}:{row_number} GateNo 超出 uint8")
                if not 0 <= depth <= np.iinfo(np.uint8).max:
                    raise ValueError(f"{path.name}:{row_number} Depth 超出 uint8")

                mileage_batch.append(mileage)
                id_batch.append(ident)
                depth_batch.append(depth)
                gate_batch.append(gate)
                track_batch.append(track)
                if len(mileage_batch) >= batch_size:
                    flush_batch()
                    try:
                        current = int(stream.tell())
                    except (OSError, ValueError):
                        current = 0
                    completed = processed_bytes + min(path.stat().st_size, current)
                    fraction = completed / max(1, total_bytes)
                    _report(
                        progress_callback,
                        f"正在转换超声 CSV：{path.name}（{fraction * 100:.1f}%）",
                    )
            flush_batch()
        processed_bytes += path.stat().st_size

    if total_rows <= 0:
        raise ValueError("超声 CSV 没有可转换的有效记录")

    chunks = []
    for key, count in sorted(chunk_counts.items()):
        filename = f"chunk_{key:05d}.bin"
        path = output_dir / filename
        expected_bytes = count * RECORD_DTYPE.itemsize
        actual_bytes = path.stat().st_size
        if actual_bytes != expected_bytes:
            raise RuntimeError(
                f"{filename}: 应为 {expected_bytes} 字节，实际为 {actual_bytes} 字节"
            )
        chunks.append(
            {
                "key": key,
                "start": key * chunk_width,
                "end": (key + 1) * chunk_width,
                "file": f"data/{filename}",
                "count": count,
                "bytes": actual_bytes,
            }
        )

    observed_gates = set(gate_counts)
    all_gates = sorted(observed_gates | set(range(1, 29)))
    manifest = {
        "version": 1,
        "dataset": dataset,
        "sourceDirectory": input_dir.as_posix(),
        "sourceFiles": [path.name for path, _segment in files],
        "totalRecords": total_rows,
        "mileageMin": mileage_min,
        "mileageMax": mileage_max,
        "idMin": id_min,
        "idMax": id_max,
        "depthMin": depth_min,
        "depthMax": depth_max,
        "chunkWidthKm": chunk_width,
        "recordSizeBytes": RECORD_DTYPE.itemsize,
        "recordLayout": [
            {"name": "mileage", "type": "float32-le", "offset": 0},
            {"name": "id", "type": "uint32-le", "offset": 4},
            {"name": "depth", "type": "uint8", "offset": 8},
            {"name": "gate", "type": "uint8", "offset": 9},
            {"name": "track", "type": "uint8", "offset": 10},
            {"name": "segment", "type": "uint8", "offset": 11},
        ],
        "trackCounts": {
            str(key): value for key, value in sorted(track_counts.items())
        },
        "gateCounts": {
            str(key): value for key, value in sorted(gate_counts.items())
        },
        "gates": [
            {"gate": gate, "name": f"Gate {gate}", "symbolId": f"gate-{gate}"}
            for gate in all_gates
        ],
        "chunks": chunks,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _report(
        progress_callback,
        f"超声 CSV 转换完成：{total_rows:,} 条记录，{len(chunks)} 个二进制分片",
    )
    return manifest


def ensure_binary_data(
    runtime_dir: Path,
    csv_dir: Path | None = None,
    *,
    dataset: str = "20251221",
    progress_callback: ProgressCallback | None = None,
    stop_callback: StopCallback | None = None,
) -> Path:
    """Ensure the configured ultrasound directory contains a binary cache.

    Existing binary data is reused.  If raw CSV is present and newer than the
    manifest, the cache is rebuilt in the same runtime directory.  Raw CSV
    files are never removed.
    """

    runtime_dir = Path(runtime_dir).resolve()
    source_dir = Path(csv_dir).resolve() if csv_dir is not None else runtime_dir
    source_files = combined_csv_files(source_dir)
    manifest_path = runtime_dir / "manifest.json"
    needs_build = not has_binary_cache(runtime_dir)
    if source_files and manifest_path.exists():
        newest_csv = max(path.stat().st_mtime for path, _segment in source_files)
        needs_build = needs_build or newest_csv > manifest_path.stat().st_mtime

    if not needs_build:
        _report(progress_callback, "已找到超声二进制缓存，直接加载")
        return runtime_dir
    if not source_files:
        return runtime_dir

    _report(progress_callback, "发现原始超声 CSV，首次加载时正在生成二进制缓存")
    convert_csv_to_binary(
        source_dir,
        runtime_dir,
        dataset=dataset,
        force=True,
        progress_callback=progress_callback,
        stop_callback=stop_callback,
    )
    return runtime_dir

