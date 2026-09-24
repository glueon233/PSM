"""Shared utilities: path safety, MIME mapping, ffmpeg detection."""

import mimetypes
import os
import shutil
import urllib.parse

MIME_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv",
    ".ts": "video/mp2t",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".dsf": "audio/x-dsf",
    ".dff": "audio/x-dff",
    ".dsd": "audio/dsd",
    ".wv": "audio/x-wavpack",
    ".ape": "audio/x-ape",
    ".opus": "audio/opus",
    ".ass": "text/x-ass",
    ".srt": "application/x-subrip",
}

MEDIA_EXTS = tuple(sorted(MIME_TYPES.keys()))
STREAMABLE_EXTS = (".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".flv", ".ts", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a")


def guess_mime(path):
    ext = os.path.splitext(path)[1].lower()
    return MIME_TYPES.get(ext, mimetypes.guess_type(path)[0] or "application/octet-stream")


def safe_join(root, rel):
    """Resolve rel under root; return None on escape attempt or missing file."""
    root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, full]) != root:
        return None
    return full


def quote_relpath(rel):
    return urllib.parse.quote(rel.replace(os.sep, "/"))


def ensure_firewall_rules(port):
    """Windows: make sure inbound access to this python on `port` (HTTP) and
    UDP 1900 (SSDP) is allowed on Private/Domain/Public profiles.

    A missing rule silently blocks LAN clients from fetching the device
    description / media even when SSDP discovery itself works. Returns a
    status string for logging."""
    if os.name != "nt":
        return ""
    import subprocess
    import sys
    pid = os.getpid()
    # Resolve the REAL executable path (sys.executable may be the Store
    # app-execution alias, which does not match the running process image).
    ps = (
        "$exe=(Get-Process -Id %d -ErrorAction SilentlyContinue).Path;"
        "if (-not $exe) { Write-Output 'NOPROC' } else {"
        "$need=$true;"
        "Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Program -eq $exe } | ForEach-Object { "
        "$r = $_ | Get-NetFirewallRule; "
        "if ($r.Direction -eq 'Inbound' -and $r.Action -eq 'Allow' -and "
        "    ($r.Profile -match 'Private' -or $r.Profile -eq 'Any')) { $need=$false } };"
        "if ($need) { "
        "New-NetFirewallRule -DisplayName 'StreamMedia HTTP' -Direction Inbound -Action Allow "
        "-Protocol TCP -LocalPort %d -Program $exe -Profile Domain,Private,Public | Out-Null;"
        "New-NetFirewallRule -DisplayName 'StreamMedia SSDP' -Direction Inbound -Action Allow "
        "-Protocol UDP -LocalPort 1900 -Program $exe -Profile Domain,Private,Public | Out-Null;"
        "Write-Output 'ADDED' } else { Write-Output 'EXISTS' } }"
    ) % (pid, port)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, timeout=60)
        if b"ADDED" in out.stdout:
            return "firewall rules added (HTTP %d / SSDP 1900)" % port
        if b"EXISTS" in out.stdout:
            return "firewall rules already present"
        if b"NOPROC" in out.stdout:
            return "firewall check skipped (cannot resolve process path)"
        return "firewall check skipped (non-admin?)"
    except Exception:
        return "firewall check failed"


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"D:\ffmpeg\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return None
