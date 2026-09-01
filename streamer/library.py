"""Media library: scan the source folder and probe metadata (pure stdlib)."""

import os
import struct

from .util import MEDIA_EXTS, safe_join


def scan_library(source_dir):
    items = []
    for dirpath, _dirnames, filenames in os.walk(source_dir):
        for name in filenames:
            if not name.lower().endswith(MEDIA_EXTS):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, source_dir)
            info = probe(full)
            items.append({
                "id": rel,
                "name": name,
                "ext": os.path.splitext(name)[1].lower(),
                "size": os.path.getsize(full),
                "duration": info.get("duration"),
                "codec": info.get("codec", "unknown"),
                "width": info.get("width"),
                "height": info.get("height"),
            })
    items.sort(key=lambda i: i["name"].lower())
    return items


def probe(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".wav":
            return _probe_wav(path)
        if ext == ".avi":
            return _probe_avi(path)
        if ext in (".mp4", ".m4v", ".mov"):
            return _probe_mp4(path)
    except Exception:
        pass
    return {}


def _probe_wav(path):
    with open(path, "rb") as f:
        if f.read(4) != b"RIFF":
            return {}
        f.read(4)  # riff size
        if f.read(4) != b"WAVE":
            return {}
        fmt = data_size = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = struct.unpack("<4sI", hdr)
            if cid == b"fmt ":
                fmt = f.read(size)
            elif cid == b"data":
                data_size = size
                break
            else:
                f.seek(size + (size % 2), 1)
        if not fmt or not data_size:
            return {}
        channels, rate, byte_rate = struct.unpack("<HII", fmt[2:12])
        return {"duration": data_size / byte_rate, "codec": "PCM%d" % fmt[14]}


def _probe_avi(path):
    with open(path, "rb") as f:
        if f.read(4) != b"RIFF":
            return {}
        f.read(4)  # riff size
        if f.read(4) != b"AVI ":
            return {}
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = struct.unpack("<4sI", hdr)
            if cid == b"LIST" and f.read(4) == b"hdrl":
                return _probe_avi_hdrl(f, size - 4)
            f.seek(size + (size % 2), 1)
    return {}


def _probe_avi_hdrl(f, size):
    end = f.tell() + size
    info = {}
    while f.tell() < end - 8:
        pos = f.tell()
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        cid, csize = struct.unpack("<4sI", hdr)
        if cid == b"LIST":
            ltype = f.read(4)
            if ltype == b"strl":
                end = pos + 8 + csize  # descend into stream list
                continue
            f.seek(pos + 8 + csize + (csize % 2))
            continue
        if cid == b"avih":
            body = f.read(min(csize, 56))
            if len(body) >= 48:
                fields = struct.unpack("<14I", body[:56])
                micro_per_frame, _maxb, _pad, _flags, total, _init, _streams = fields[:7]
                w, h = fields[8], fields[9]
                info = {"duration": total * micro_per_frame / 1e6, "width": w, "height": h}
        elif cid == b"strh" and "codec" not in info:
            body = f.read(min(csize, 56))
            if len(body) >= 8:
                info["codec"] = body[4:8].decode("latin1").strip() or "DIB"
        f.seek(pos + 8 + csize + (csize % 2))
    return info


def _probe_mp4(path):
    with open(path, "rb") as f:
        end = os.path.getsize(path)
        while f.tell() < end - 8:
            header_len = 8
            size, ctype = struct.unpack(">I4s", f.read(8))
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
                header_len = 16
            if size < 8:
                break
            if ctype == b"moov":
                return _probe_mp4_moov(f, size - header_len)
            f.seek(size - header_len, 1)
    return {}


def _probe_mp4_moov(f, size):
    end = f.tell() + size
    while f.tell() < end - 8:
        bsize, btype = struct.unpack(">I4s", f.read(8))
        if bsize < 8:
            break
        if btype == b"mvhd":
            body = f.read(bsize - 8)
            version = body[0]
            if version == 1:
                timescale, duration = struct.unpack(">IQ", body[20:32])
            else:
                timescale, duration = struct.unpack(">II", body[12:20])
            return {"duration": duration / timescale, "codec": "h264/aac"}
        f.seek(bsize - 8, 1)
    return {}


def resolve_source(source_dir, media_id):
    """Validate a media id and return the absolute path, or None."""
    full = safe_join(source_dir, media_id)
    if not full or not os.path.isfile(full):
        return None
    return full
