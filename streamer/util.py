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


def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None
