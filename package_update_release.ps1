[CmdletBinding()]
param(
    [string]$SoftwareDirectory = (Join-Path $PSScriptRoot '分发软件版本\软件'),
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'release-assets')
)

$ErrorActionPreference = 'Stop'
$SoftwareDirectory = [System.IO.Path]::GetFullPath($SoftwareDirectory)
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$executable = Join-Path $SoftwareDirectory 'UltrasoundMFLReplay.exe'
$internalDirectory = Join-Path $SoftwareDirectory '_internal'
$versionSource = Join-Path $PSScriptRoot 'version_update.py'

if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "找不到程序：$executable。请先运行 build_desktop.ps1 完成打包。"
}
if (-not (Test-Path -LiteralPath $internalDirectory -PathType Container)) {
    throw "找不到程序依赖目录：$internalDirectory。"
}
if (-not (Test-Path -LiteralPath $versionSource -PathType Leaf)) {
    throw "找不到版本信息：$versionSource。"
}

$sourceText = Get-Content -LiteralPath $versionSource -Raw -Encoding UTF8
$versionMatch = [regex]::Match($sourceText, 'APP_VERSION\s*=\s*"([^"]+)"')
if (-not $versionMatch.Success) {
    throw '无法从 version_update.py 读取 APP_VERSION。'
}
$version = $versionMatch.Groups[1].Value
$stage = Join-Path $env:TEMP ('MFLReplay-release-' + [guid]::NewGuid().ToString('N'))
$archiveName = 'UltrasoundMFLReplay-windows-x64.zip'
$archivePath = Join-Path $OutputDirectory $archiveName
$temporaryArchive = "$archivePath.tmp.zip"

try {
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $stage | Out-Null
    Copy-Item -LiteralPath $executable -Destination $stage
    Copy-Item -LiteralPath $internalDirectory -Destination $stage -Recurse

    # PyInstaller stores bundled source data under _internal. The app uses the
    # user-editable config beside the EXE, so never ship the build machine's
    # dataset paths or alignment anchors inside the updater archive.
    $bundledConfig = Join-Path $stage '_internal\config.json'
    if (Test-Path -LiteralPath $bundledConfig -PathType Leaf) {
        Remove-Item -LiteralPath $bundledConfig -Force
    }

    if (Test-Path -LiteralPath $temporaryArchive) {
        Remove-Item -LiteralPath $temporaryArchive -Force
    }
    Compress-Archive -Path (Join-Path $stage '*') `
        -DestinationPath $temporaryArchive -CompressionLevel Optimal
    Move-Item -LiteralPath $temporaryArchive -Destination $archivePath -Force

    $hash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "版本：$version"
    Write-Host "Release 标签：v$version"
    Write-Host "更新包：$archivePath"
    Write-Host "SHA-256：$hash"
    Write-Host '将此 ZIP 作为 GitHub Release 资产上传，文件名须保持不变。'
    Write-Host 'ZIP 根目录只包含 UltrasoundMFLReplay.exe 和 _internal/；不要放入 config.json、数据或标注。'
} finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $temporaryArchive) {
        Remove-Item -LiteralPath $temporaryArchive -Force -ErrorAction SilentlyContinue
    }
}
