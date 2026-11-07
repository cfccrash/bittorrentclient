"""Windows Defender Firewall helper.

A forwarded port is useless if Windows Firewall drops the connection once it reaches this PC. Windows normally asks
on first run and creates a rule from your answer (Cancel = a BLOCK rule). This module looks for rules for this
program (by path, so Windows' own rules count) and can replace them with one clear allow rule - one UAC prompt."""
import base64
import os
import subprocess
import sys

RULE = "Bit Torrent Client"
NO_WINDOW = 0x08000000


def available():
    return sys.platform == "win32"


def program_path():
    """The program Windows sees: the .exe when frozen, otherwise the Python interpreter running the script."""
    return os.path.abspath(sys.executable)


def _q(path):
    return path.replace("'", "''")   # PowerShell single-quoted string


def check_script(exe):
    return ("$p='%s'; "
            "$r = @(Get-NetFirewallApplicationFilter -Program $p -ErrorAction SilentlyContinue | "
            "Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.Direction -eq 'Inbound' -and $_.Enabled -eq 'True' }); "
            "if ($r | Where-Object { $_.Action -eq 'Block' }) { 'BLOCKED' } "
            "elseif ($r | Where-Object { $_.Action -eq 'Allow' }) { 'ALLOWED' } else { 'MISSING' }" % _q(exe))


def fix_script(exe):
    """Remove every inbound rule for this program (including a Block rule from a dismissed prompt) and add one allow rule."""
    return ("$p='%s'; "
            "Get-NetFirewallApplicationFilter -Program $p -ErrorAction SilentlyContinue | "
            "Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.Direction -eq 'Inbound' } | "
            "Remove-NetFirewallRule -ErrorAction SilentlyContinue; "
            "New-NetFirewallRule -DisplayName '%s' -Direction Inbound -Action Allow -Program $p -Profile Any -Enabled True | Out-Null"
            % (_q(exe), RULE))


def encode(script):
    """PowerShell -EncodedCommand payload (UTF-16LE base64): no quoting problems whatever the path contains."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def parse_check(output):
    word = output.strip().upper()
    return {"ALLOWED": "allowed", "BLOCKED": "blocked", "MISSING": "missing"}.get(word)


def check():
    """'allowed' | 'blocked' | 'missing' for this program, or None if it cannot be determined."""
    if not available():
        return None
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encode(check_script(program_path()))],
                           capture_output=True, text=True, errors="replace", timeout=25, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_check(r.stdout) if r.returncode == 0 else None


def add_rule():
    """Replace this program's inbound rules with one allow rule (Windows shows a UAC prompt). True if the elevated
    command was started, False if the user declined or this is not Windows."""
    if not available():
        return False
    try:
        import ctypes
        params = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand " + encode(fix_script(program_path()))
        return ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", params, None, 0) > 32
    except Exception:
        return False
