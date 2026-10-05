"""Offscreen regression checks; all annotation writes use a temporary directory."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
import sys
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from annotation_store import AnnotationStore, ANNOTATION_LABELS, MFL_RECORD_DTYPE, MFL_CHANNELS
from model_result_store import ModelResultStore
from server import MFLDataset
import numpy as np
from desktop_app import ReplayWindow, DataWorker


def main():
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "config.json"
        config.write_text(json.dumps({"dataset": "test", "annotation_dir": "annotations", "mfl_data_dir": "raw"}))
        store = AnnotationStore(config)
        raw = Path(directory) / "raw" / "EX01"
        raw.mkdir(parents=True)
        records = np.zeros(32, dtype=MFL_RECORD_DTYPE)
        records["index"] = np.arange(32)
        records["value"] = np.arange(32) * 2
        for channel in MFL_CHANNELS:
            records.tofile(raw / (channel + ".dat"))
        span = dict(name="EX01", recordStart=10, recordEnd=20,
                    sourceIndexStart=10, sourceIndexEnd=20, xStartM=1, xEndM=2)
        for code in ANNOTATION_LABELS:
            event_id = store.save_event([span], selected_start_m=1, selected_end_m=2, label=code)
            store.save_event([span], selected_start_m=1, selected_end_m=2,
                             label=code, annotation_id=event_id)
            result = store.read_event_values(event_id)
            assert result["label"] == code
            assert result["spans"][0]["values"].shape == (32, 11)
            assert result["spans"][0]["values"][0, 0] == 20
            del result
        assert len(store.load_event_groups()) == 5
        model_csv = Path(directory) / "model_results.csv"
        model_csv.write_text(
            "event_id,start_record,end_record,predicted_label,confidence,dominant_side\n"
            "E_TEST,10,20,斜裂纹,0.91,Left\n",
            encoding="utf-8-sig",
        )
        mfl_dataset = MFLDataset(
            raw.parent,
            1.0,
            {"EX01": 0.0},
            {"mfl_segment": "EX01", "mfl_record_pos": 0, "mfl_raw_index": 0},
        )
        model_store = ModelResultStore(model_csv, mfl_dataset)
        assert len(model_store.predictions) == 1
        model_prediction = model_store.predictions[0]
        assert model_prediction["label"] == "model_anomaly"
        assert model_prediction["label_name"] == "模型异常候选"
        assert model_prediction["scope"] == "left"
        assert model_prediction["x_start_m"] == 10
        assert model_prediction["x_end_m"] == 20
        frequency_csv = Path(directory) / "frequency.csv"
        frequency_csv.write_text(
            "event_id,rail,start_record,end_record,peak_anomaly_score,frequency_hint\n"
            "W,left,10,20,15,weld\n"
            "V,right,10,20,1,vibration_interference\n", encoding="utf-8-sig",
        )
        frequency_store = ModelResultStore(frequency_csv, mfl_dataset)
        assert {p["label_name"] for p in frequency_store.predictions} == {"焊缝候选", "振动干扰候选"}
        assert all(p["label"] == "model_anomaly" for p in frequency_store.predictions)
        del frequency_store
        with frequency_csv.open('a', encoding='utf-8') as handle:
            handle.write('C,right,10,20,3,suspected_oblique_crack\n')
        crack_store = ModelResultStore(frequency_csv, mfl_dataset)
        assert any(p['label_name']=='疑似斜裂纹候选' for p in crack_store.predictions)
        del crack_store
        with patch.object(DataWorker, "start"):
            window = ReplayWindow(config)
        window.manifest = {"range": {"startM": 0, "endM": 100}}
        window.manifest["range"]["endM"] = 500
        window.back_100_button.setEnabled(True)
        window.forward_100_button.setEnabled(True)
        with patch.object(window, "request_window"):
            window.forward_100_button.click()
            assert window.view_range() == (100, 108)
            window.back_100_button.click()
            window.back_100_button.click()
            assert window.view_range() == (0, 8)
            window.set_position(490)
            window.forward_100_button.click()
            assert window.view_range() == (492, 500)
            window.set_position(0)
        window.show()
        app.processEvents()
        assert window.annotation_label_combo.count() == 5
        assert window.annotation_jump_combo.count() == 6
        assert window.model_result_filter_combo.findData('frequency_hint:suspected_oblique_crack') >= 0
        window.measure_button.setChecked(True)
        app.processEvents()
        pixel_tolerance = 0.0
        for plot, value in ((window.us_plot, 2), (window.mfl_plot, 3)):
            app.processEvents()
            geometry = plot.geometry_info()
            # Mouse coordinates are integer pixels; small screens increase metres/pixel.
            pixel_tolerance += abs(plot.pixel_to_x(1, geometry) - plot.pixel_to_x(0, geometry)) / 2
            QTest.mouseClick(plot, Qt.LeftButton,
                             pos=QPoint(round(plot.x_to_pixel(value, geometry)), 40))
        assert len(window.measure_points) == 2
        assert abs(abs(window.measure_points[1] - window.measure_points[0]) - 1) <= pixel_tolerance + 1e-9, window.measure_points
        assert window.annotation_edit_id is None
        window.add_measure_point(4)
        assert window.measure_points == [4]
        window.add_measure_point(3.5)
        assert "500.0 mm" in window.measure_label.text()
        window.annotation_mode_button.setChecked(True)
        assert not window.measure_mode
        window.annotation_selection_start_m = 3
        window.annotation_selection_end_m = 4
        window.us_plot.grab()
        window.mfl_plot.grab()
        if len(sys.argv) > 1 and sys.argv[1] != "--exe-check":
            window.grab().save(sys.argv[1])
        window.close()
        if "--exe-check" in sys.argv:
            distribution = Path(__file__).resolve().parents[1] / "分发软件版本"
            settings = json.loads(config.read_text())
            settings["ultrasound_data_dir"] = str(distribution / "data/20251221/超声/data")
            settings["joint"] = {"mfl_segment": "EX01", "mfl_record_pos": 0, "mfl_raw_index": 0, "us_id": 2794006}
            config.write_text(json.dumps(settings))
            completed = subprocess.run([str(distribution / "软件/UltrasoundMFLReplay.exe"),
                                        "--config", str(config), "--smoke-test"], timeout=60)
            assert completed.returncode == 0, completed.returncode
            print("PASS: packaged EXE loads synthetic DAT and real ultrasound cache")
        del model_store
        del mfl_dataset
    print("PASS: five label types persist/update; model result mapping; cross-modal two-click measurement; mode isolation; plot rendering")


if __name__ == "__main__":
    main()
