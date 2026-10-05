[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$software = Join-Path $PSScriptRoot 'portable-release\软件'
$stage = Join-Path $PSScriptRoot 'build\staging'
python -m PyInstaller --noconfirm --clean --onedir --windowed --name UltrasoundMFLReplay --distpath $stage --add-data 'assets;assets' desktop_app.py
if ($LASTEXITCODE -ne 0) { throw '程序打包失败；正式分发程序未替换。' }
New-Item -ItemType Directory -Path $software -Force | Out-Null
$config = Join-Path $software 'config.json'
if (-not (Test-Path -LiteralPath $config)) {
    $settings = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $settings.mfl_data_dir = '../data/20251221/漏磁'
    $settings.ultrasound_data_dir = '../data/20251221/超声/data'
    $settings.annotation_dir = '../annotations'
    $settings.model_result_path = '../model_results/crack_coherence_preview_v3.csv'
    $settings | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $config -Encoding UTF8
}
$internal = Join-Path $software '_internal'
if (Test-Path -LiteralPath $internal) { Remove-Item -LiteralPath $internal -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $stage 'UltrasoundMFLReplay\_internal') -Destination $software -Recurse
Copy-Item -LiteralPath (Join-Path $stage 'UltrasoundMFLReplay\UltrasoundMFLReplay.exe') -Destination $software -Force
Remove-Item -LiteralPath $stage -Recurse -Force
Write-Host "完成：$software\UltrasoundMFLReplay.exe"
Write-Host '直接拷走整个portable-release目录即可；已有配置、数据及标注已保留。'
