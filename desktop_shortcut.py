"""Create a Windows shortcut using the actual (possibly redirected) Desktop."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys


def create_desktop_shortcut(data_directory, *, desktop=None, executable=None, replace_existing=False):
    if os.name != 'nt':
        raise OSError('Ярлык поддерживается только в Windows.')
    frozen = getattr(sys, 'frozen', False)
    target = Path(sys.executable).resolve()
    if executable is not None:
        target = Path(executable).resolve()
        if not target.is_file() or target.suffix.lower() != '.exe':
            raise OSError('Не найден установленный EXE')
        frozen = True
    arguments = []
    if not frozen:
        windowed = target.with_name('pythonw.exe')
        if windowed.exists():
            target = windowed
        arguments.append(str(Path(__file__).resolve().with_name('app.py')))
    arguments += ['--data-dir', str(Path(data_directory).resolve())]
    payload = base64.b64encode(json.dumps({
        'target': str(target), 'arguments': subprocess.list2cmdline(arguments),
        'working': str(target.parent if frozen else Path(__file__).resolve().parent),
        'desktop': str(Path(desktop).resolve()) if desktop else None,
        'replace': bool(replace_existing),
    }).encode('utf-8')).decode('ascii')
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$data = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')) | ConvertFrom-Json
$shell = New-Object -ComObject WScript.Shell
$folder = if ($data.desktop) { $data.desktop } else { $shell.SpecialFolders.Item('Desktop') }
if (-not (Test-Path -LiteralPath $folder -PathType Container)) { throw 'Desktop unavailable' }
$name = 'Telegram Offer Scheduler'
$index = 1
while ($true) {
    $suffix = if ($index -eq 1) { '' } else { ' (' + $index + ')' }
    $path = Join-Path $folder ($name + $suffix + '.lnk')
    if (-not (Test-Path -LiteralPath $path)) { break }
    $existing = $shell.CreateShortcut($path)
    if ($data.replace -and $existing.Description -eq 'Telegram Offer Scheduler') {
        if ($existing.Arguments -match '(--data-dir\s+.+)$') { $data.arguments = $Matches[1] }
        break
    }
    if ($existing.TargetPath -eq $data.target -and $existing.Arguments -eq $data.arguments) { break }
    $index++
}
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath = $data.target
$shortcut.Arguments = $data.arguments
$shortcut.WorkingDirectory = $data.working
$shortcut.IconLocation = $data.target + ',0'
$shortcut.Description = 'Telegram Offer Scheduler'
$shortcut.Save()
[Console]::Write($path)
'''.replace('PAYLOAD', payload)
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise OSError('Windows не завершила создание ярлыка вовремя. Проверьте рабочий стол.') from exc
    if result.returncode:
        raise OSError('Не удалось создать ярлык. Проверьте доступ к рабочему столу и разрешение запуска Windows PowerShell.')
    path = Path(result.stdout.decode('utf-8').strip())
    if not path.is_file() or path.suffix.lower() != '.lnk':
        raise OSError('Windows не подтвердила создание ярлыка.')
    return path
