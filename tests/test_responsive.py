"""Offscreen viewport/DPI regression; does not read or modify annotations."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
import tempfile
from pathlib import Path
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication,QScrollArea,QPushButton
from PyQt5.QtGui import QFontDatabase
from desktop_app import ReplayWindow,DataWorker,configure_application

QApplication.setAttribute(Qt.AA_EnableHighDpiScaling,True)
app=QApplication([])
QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
configure_application(app)
with tempfile.TemporaryDirectory() as folder:
    config=Path(folder)/'config.json'
    config.write_text('{"dataset":"test","annotation_dir":"annotations"}')
    with patch.object(DataWorker,'start'):
        win=ReplayWindow(config)
    win.show()
    for width,height in [(640,480),(800,600),(1024,640),(1280,720),(1920,1080)]:
        win.resize(width,height)
        for page in range(win.tool_stack.count()):
            win.set_tool_page(page)
            app.processEvents()
            assert win.width()<=width and win.height()<=height,(width,height,win.size())
            scroll=win.tool_stack.widget(page)
            for button in scroll.findChildren(QPushButton):
                assert button.width()>=button.minimumWidth()
                assert button.height()>=button.sizeHint().height(),button.text()
            assert win.us_plot.width()>=280 and win.mfl_plot.width()>=280
    win.resize(800,600);win.set_tool_page(1);app.processEvents()
    output=Path(__file__).resolve().parents[1]/'outputs/responsive_test'
    output.mkdir(parents=True,exist_ok=True)
    win.grab().save(str(output/f"800x600_scale_{os.environ.get('QT_SCALE_FACTOR','1')}.png"))
    win.close()
print('PASS five viewport sizes, all tool pages, button minimums, DPI',os.environ.get('QT_SCALE_FACTOR','1'))
