"""GitHub Releases based updater for the Windows portable desktop app."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from PyQt5.QtCore import QThread, pyqtSignal


# Date-based release version with a numeric patch suffix for rebuilds.
APP_VERSION = "2026.10.05.1"
DEFAULT_GITHUB_REPOSITORY = "Polaris-civil/UltrasoundMFLReplay"
UPDATE_ASSET_NAME = "UltrasoundMFLReplay-windows-x64.zip"
GITHUB_API_VERSION = "2022-11-28"
MAX_UPDATE_UNPACKED_BYTES = 12 * 1024 * 1024 * 1024


class UpdateError(RuntimeError):
    """An actionable error while checking, downloading, or staging an update."""


def validate_repository(value: str) -> str:
    repository = str(value or "").strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise UpdateError("GitHub 仓库格式应为 owner/repository。")
    return repository


def _version_key(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(value or ""))
    if not numbers:
        raise UpdateError(f"版本号“{value}”中没有数字，无法比较版本。")
    return tuple(int(number) for number in numbers)


def check_latest_release(repository: str, current_version: str) -> dict[str, Any]:
    repository = validate_repository(repository)
    url = f"https://api.github.com/repos/{repository}/releases/latest"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"UltrasoundMFLReplay-Updater/{current_version}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            release = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("仓库不存在，或还没有发布正式 Release。") from exc
        if exc.code == 403:
            raise UpdateError("GitHub 暂时限制了更新查询，请稍后再试。") from exc
        raise UpdateError(f"GitHub 查询失败（HTTP {exc.code}）。") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"无法连接 GitHub：{exc.reason}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("GitHub 返回了无法识别的版本信息。") from exc

    if not isinstance(release, dict) or not release.get("tag_name"):
        raise UpdateError("GitHub Release 缺少版本标签。")
    tag = str(release["tag_name"]).strip()
    available = _version_key(tag) > _version_key(current_version)
    result: dict[str, Any] = {
        "repository": repository,
        "tag": tag,
        "name": str(release.get("name") or tag),
        "body": str(release.get("body") or "").strip(),
        "html_url": str(release.get("html_url") or ""),
        "published_at": str(release.get("published_at") or ""),
        "update_available": available,
    }
    if not available:
        return result

    assets = release.get("assets")
    if not isinstance(assets, list):
        assets = []
    asset = next(
        (
            item
            for item in assets
            if isinstance(item, dict)
            and item.get("name") == UPDATE_ASSET_NAME
            and item.get("state", "uploaded") == "uploaded"
        ),
        None,
    )
    if asset is None:
        raise UpdateError(
            f"发现新版本 {tag}，但 Release 中没有更新包 {UPDATE_ASSET_NAME}。"
        )
    download_url = str(asset.get("browser_download_url") or "")
    parsed_url = urlparse(download_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "github.com":
        raise UpdateError("Release 更新包地址不是有效的 GitHub HTTPS 地址。")
    digest = str(asset.get("digest") or "").strip().lower()
    if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        digest = ""
    result.update(
        {
            "asset_name": UPDATE_ASSET_NAME,
            "asset_url": download_url,
            "asset_size": max(0, int(asset.get("size") or 0)),
            "asset_sha256": digest.removeprefix("sha256:") if digest else "",
        }
    )
    return result


class ReleaseCheckWorker(QThread):
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, repository: str, current_version: str) -> None:
        super().__init__()
        self.repository = repository
        self.current_version = current_version

    def run(self) -> None:
        try:
            self.succeeded.emit(
                check_latest_release(self.repository, self.current_version)
            )
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # keep unexpected network errors out of Qt
            self.failed.emit(f"检查更新失败：{exc}")


class ReleaseDownloadWorker(QThread):
    progress = pyqtSignal(int, int)
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, release: dict[str, Any]) -> None:
        super().__init__()
        self.release = dict(release)

    def run(self) -> None:
        download_url = str(self.release.get("asset_url") or "")
        parsed_url = urlparse(download_url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "github.com":
            self.failed.emit("更新包下载地址无效。")
            return

        target_dir = Path(tempfile.gettempdir()) / "UltrasoundMFLReplay" / "updates"
        target = target_dir / f"update-{self.release.get('tag', 'latest')}.zip"
        temporary = target.with_suffix(".download")
        request = urllib.request.Request(
            download_url,
            headers={"User-Agent": f"UltrasoundMFLReplay-Updater/{APP_VERSION}"},
        )
        expected_size = int(self.release.get("asset_size") or 0)
        expected_sha256 = str(self.release.get("asset_sha256") or "").lower()
        digest = hashlib.sha256()
        received = 0
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            temporary.unlink(missing_ok=True)
            with urllib.request.urlopen(request, timeout=30) as response:
                response_size = int(response.headers.get("Content-Length") or 0)
                if expected_size and response_size and expected_size != response_size:
                    raise UpdateError("更新包大小与 GitHub Release 信息不一致。")
                total = expected_size or response_size
                with temporary.open("wb") as stream:
                    while True:
                        if self.isInterruptionRequested():
                            raise UpdateError("更新下载已取消。")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        stream.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        self.progress.emit(received, total)
            if total and received != total:
                raise UpdateError("更新包下载不完整，请重新检查更新。")
            actual_digest = digest.hexdigest()
            if expected_sha256 and actual_digest != expected_sha256:
                raise UpdateError("更新包 SHA-256 校验失败，已取消安装。")
            os.replace(temporary, target)
            self.succeeded.emit(str(target))
        except (OSError, urllib.error.URLError, UpdateError) as exc:
            temporary.unlink(missing_ok=True)
            self.failed.emit(str(exc))
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            self.failed.emit(f"更新包下载失败：{exc}")


def validate_update_archive(archive_path: Path) -> None:
    """Reject unsafe archives and packages that could overwrite user data."""
    archive_path = Path(archive_path)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names: set[str] = set()
            unpacked_size = 0
            for info in archive.infolist():
                normalized = info.filename.replace("\\", "/")
                path = PurePosixPath(normalized)
                if (
                    not normalized
                    or path.is_absolute()
                    or ".." in path.parts
                    or any(":" in part for part in path.parts)
                ):
                    raise UpdateError("更新包包含不安全的文件路径。")
                if not info.is_dir():
                    if info.flag_bits & 0x1:
                        raise UpdateError("更新包中的加密文件无法安全安装。")
                    if path.parts != ("UltrasoundMFLReplay.exe",) and (
                        not path.parts or path.parts[0] != "_internal"
                    ):
                        raise UpdateError(
                            "更新包只能包含 EXE 和 _internal 程序文件夹。"
                        )
                    unix_mode = info.external_attr >> 16
                    if unix_mode & 0o170000 == 0o120000:
                        raise UpdateError("更新包包含不支持的符号链接。")
                    unpacked_size += int(info.file_size)
                    if unpacked_size > MAX_UPDATE_UNPACKED_BYTES:
                        raise UpdateError("更新包解压后的体积超出安全限制。")
                    names.add(normalized)
            if "UltrasoundMFLReplay.exe" not in names:
                raise UpdateError("更新包缺少 UltrasoundMFLReplay.exe。")
            if not any(name.startswith("_internal/") for name in names):
                raise UpdateError("更新包缺少 _internal 程序文件夹。")
    except zipfile.BadZipFile as exc:
        raise UpdateError("下载文件不是有效的 ZIP 更新包。") from exc
    except OSError as exc:
        raise UpdateError(f"无法读取更新包：{exc}") from exc


_POWERSHELL_INSTALLER = r'''param([string]$EncodedPayload)
$ErrorActionPreference = 'Stop'
$logPath = Join-Path $env:TEMP 'UltrasoundMFLReplay-update.log'
$stage = $null
$backup = $null
$appDir = $null
$exePath = $null
$payload = $null

function Quote-WindowsArgument([string]$Value) {
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]'\') { $slashes++; continue }
        if ($character -eq [char]'"') {
            [void]$builder.Append(('\' * ($slashes * 2 + 1)))
            [void]$builder.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) {
            [void]$builder.Append(('\' * $slashes))
            $slashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($slashes -gt 0) { [void]$builder.Append(('\' * ($slashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

try {
    Add-Content -LiteralPath $logPath -Value ("更新安装程序启动：" + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -Encoding UTF8
    $payloadJson = [System.Text.Encoding]::UTF8.GetString(
        [System.Convert]::FromBase64String($EncodedPayload)
    )
    $payload = $payloadJson | ConvertFrom-Json
    $appDir = [System.IO.Path]::GetFullPath([string]$payload.app_dir)
    $exePath = Join-Path $appDir 'UltrasoundMFLReplay.exe'
    $archivePath = [System.IO.Path]::GetFullPath([string]$payload.archive)
    Add-Content -LiteralPath $logPath -Value ("等待程序退出：" + $exePath) -Encoding UTF8

    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
        $running = Get-Process -Id ([int]$payload.process_id) -ErrorAction SilentlyContinue
        if (-not $running) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Get-Process -Id ([int]$payload.process_id) -ErrorAction SilentlyContinue) {
        throw '等待回放软件退出超时。'
    }

    $stage = Join-Path $env:TEMP ('MFLReplay-stage-' + [guid]::NewGuid().ToString('N'))
    $backup = Join-Path $appDir ('.update-backup-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $stage | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $stage -Force
    Add-Content -LiteralPath $logPath -Value '更新包已解压，开始替换程序文件。' -Encoding UTF8
    $newExe = Join-Path $stage 'UltrasoundMFLReplay.exe'
    $newInternal = Join-Path $stage '_internal'
    if (-not (Test-Path -LiteralPath $newExe -PathType Leaf) -or
        -not (Test-Path -LiteralPath $newInternal -PathType Container)) {
        throw '更新包目录结构不正确。'
    }

    New-Item -ItemType Directory -Path $backup | Out-Null
    $backupExe = Join-Path $backup 'UltrasoundMFLReplay.exe'
    $backupInternal = Join-Path $backup '_internal'
    $movedOldExe = $false
    $movedOldInternal = $false
    $installedExe = $false
    $installedInternal = $false
    try {
        if (Test-Path -LiteralPath $exePath) {
            Move-Item -LiteralPath $exePath -Destination $backupExe
            $movedOldExe = $true
        }
        $internalPath = Join-Path $appDir '_internal'
        if (Test-Path -LiteralPath $internalPath) {
            Move-Item -LiteralPath $internalPath -Destination $backupInternal
            $movedOldInternal = $true
        }
        Move-Item -LiteralPath $newExe -Destination $exePath
        $installedExe = $true
        Move-Item -LiteralPath $newInternal -Destination (Join-Path $appDir '_internal')
        $installedInternal = $true

        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $exePath
        $psi.WorkingDirectory = $appDir
        $quotedArguments = @($payload.arguments | ForEach-Object {
            Quote-WindowsArgument ([string]$_)
        })
        $psi.Arguments = [string]::Join(' ', [string[]]$quotedArguments)
        [void][System.Diagnostics.Process]::Start($psi)
        Add-Content -LiteralPath $logPath -Value '程序文件已替换，已启动新版程序。' -Encoding UTF8
    } catch {
        if ($installedExe -and (Test-Path -LiteralPath $exePath)) {
            Remove-Item -LiteralPath $exePath -Force
        }
        $internalPath = Join-Path $appDir '_internal'
        if ($installedInternal -and (Test-Path -LiteralPath $internalPath)) {
            Remove-Item -LiteralPath $internalPath -Recurse -Force
        }
        if ($movedOldExe -and (Test-Path -LiteralPath $backupExe)) {
            Move-Item -LiteralPath $backupExe -Destination $exePath
        }
        if ($movedOldInternal -and (Test-Path -LiteralPath $backupInternal)) {
            Move-Item -LiteralPath $backupInternal -Destination $internalPath
        }
        Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }

    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
} catch {
    $message = '软件自动更新失败：' + $_.Exception.Message
    try { Set-Content -LiteralPath $logPath -Value $message -Encoding UTF8 } catch { }
    if ($exePath -and (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        try {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = $exePath
            $psi.WorkingDirectory = $appDir
            $quotedArguments = @($payload.arguments | ForEach-Object {
                Quote-WindowsArgument ([string]$_)
            })
            $psi.Arguments = [string]::Join(' ', [string[]]$quotedArguments)
            [void][System.Diagnostics.Process]::Start($psi)
        } catch { }
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            ($message + "`n详细信息：" + $logPath),
            '软件更新失败',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        ) | Out-Null
    } catch { }
} finally {
    if ($stage -and (Test-Path -LiteralPath $stage)) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $PSCommandPath) {
        Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
    }
}
'''


def start_windows_update_installer(archive_path: Path) -> None:
    """Start a detached PowerShell helper that replaces files after app exit."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        raise UpdateError("自动安装只适用于已打包的 Windows 便携版。")
    executable = Path(sys.executable).resolve()
    if executable.name.lower() != "ultrasoundmflreplay.exe":
        raise UpdateError("当前程序路径与预期的便携版程序不匹配，已取消安装。")
    payload = {
        "app_dir": str(executable.parent),
        "archive": str(Path(archive_path).resolve()),
        "process_id": os.getpid(),
        "arguments": list(sys.argv[1:]),
    }
    encoded_payload = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    script_path = Path(tempfile.gettempdir()) / (
        f"UltrasoundMFLReplay-update-{os.getpid()}.ps1"
    )
    script_path.write_text(_POWERSHELL_INSTALLER, encoding="utf-8-sig")
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = 0
    # DETACHED_PROCESS caused Windows PowerShell to exit without running the
    # -File script on some systems. The hidden window and a new process group
    # keep the helper out of the user's way while it waits for this app to exit.
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP
    log_path = Path(tempfile.gettempdir()) / "UltrasoundMFLReplay-update.log"
    try:
        with log_path.open("a", encoding="utf-8") as log_stream:
            log_stream.write("正在启动 Windows 更新安装程序。\n")
            log_stream.flush()
            subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script_path),
                    encoded_payload,
                ],
                cwd=str(executable.parent),
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=log_stream,
                startupinfo=startup_info,
                creationflags=creation_flags,
                close_fds=True,
            )
    except OSError as exc:
        script_path.unlink(missing_ok=True)
        raise UpdateError(
            f"无法启动更新安装程序：{exc}\n安装日志：{log_path}"
        ) from exc
