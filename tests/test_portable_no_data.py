"""Check the data-free package from an unrelated path without Python in PATH."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
package=ROOT/'outputs/portable-test-package'
package.parent.mkdir(parents=True,exist_ok=True)
if not package.exists():
    shutil.copytree(ROOT/'portable-release/软件',package)
for config_path in [package/'config.json',package/'_internal/config.json']:
    if config_path.exists():
        settings=json.loads(config_path.read_text(encoding='utf-8'))
        settings.update(mfl_data_dir='data/mfl',ultrasound_data_dir='data/ultrasound',ultrasound_csv_dir='data/csv',annotation_dir='annotations',model_result_path='')
        config_path.write_text(json.dumps(settings),encoding='utf-8')
assert (package/'UltrasoundMFLReplay.exe').is_file()
for config in [package/'config.json']:
    c=json.loads(config.read_text(encoding='utf-8'))
    assert not c['model_result_path']
    assert all(not Path(c[k]).is_absolute() for k in ['mfl_data_dir','ultrasound_data_dir','ultrasound_csv_dir','annotation_dir'])
assert not any(p.suffix.lower() in {'.dat','.bin','.csv','.npz'} for p in package.rglob('*') if p.is_file())
root=Path(tempfile.mkdtemp(prefix='portable_0917_',dir=ROOT/'outputs')).resolve()
relocated=root/'异地路径 with spaces'/'软件'
shutil.copytree(package,relocated)
env=os.environ.copy()
for key in ('PYTHONHOME','PYTHONPATH','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH','QTDIR'):
    env.pop(key,None)
system_root=next((value for key,value in env.items() if key.upper()=='SYSTEMROOT'),'C:/Windows')
env['PATH']=str(Path(system_root)/'System32')
env['QT_QPA_PLATFORM']='offscreen'
proc=subprocess.Popen([str(relocated/'UltrasoundMFLReplay.exe')],cwd=root,env=env)
try:
    try:
        code=proc.wait(timeout=6)
        raise AssertionError(f'EXE exited unexpectedly: {code}')
    except subprocess.TimeoutExpired:
        print('PASS relocated packaged EXE stays running without data or Python PATH')
finally:
    # Only terminate the child created by this test, never a user replay process.
    if proc.poll() is None:
        proc.terminate();proc.wait(timeout=10)

# Verify that the actual window class handles the missing-data signal gracefully.
os.environ['QT_QPA_PLATFORM']='offscreen'
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer
from desktop_app import ReplayWindow
app=QApplication([])
window=ReplayWindow(relocated/'config.json');window.show()
QTimer.singleShot(2000,app.quit)
app.exec_()
assert window.isVisible()
assert window.manifest is None
assert '数据初始化失败' in window.health_label.text(),window.health_label.text()
assert not window.play_button.isEnabled()
window.close()
print('PASS missing-data UI opens with diagnostic and disables playback')
print('Package bytes',sum(p.stat().st_size for p in package.rglob('*') if p.is_file()))
print('Test copy retained:',root)
