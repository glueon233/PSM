"""Hi-res audio metadata: FLAC streaminfo + Vorbis comments, DSF/DFF headers, ID3v2.

Pure stdlib parsers used for probing duration/bitrate and DIDL tag enrichment
(artist/album/genre/…) so AIMP can display full hi-fi metadata.
"""

import os
import struct

# ---------- FLAC ----------

FLAC_STREAMINFO = 0
FLAC_VORBIS_COMMENT = 4


def iter_flac_blocks(data, offset=4):
    """Yield (block_type, block_data) starting right after 'fLaC'."""
    pos = offset
    while pos + 4 <= len(data):
        header = data[pos]
        last = bool(header & 0x80)
        btype = header & 0x7F
        size = int.from_bytes(data[pos + 1:pos + 4], "big")
        if pos + 4 + size > len(data):
            break
        yield btype, data[pos + 4:pos + 4 + size]
        if last:
            break
        pos += 4 + size


def parse_flac_streaminfo(data):
    if len(data) < 34:
        return {}
    bits = int.from_bytes(data[:18], "big")
    sample_rate = (bits >> 44) & 0xFFFFF
    channels = ((bits >> 41) & 0x7) + 1
    bps = ((bits >> 36) & 0x1F) + 1
    total = bits & ((1 << 36) - 1)
    return {"sample_rate": sample_rate, "channels": channels,
            "bits": bps, "total_samples": total}


def parse_vorbis_comments(data):
    tags = {}
    try:
        pos = 0
        vendor_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4 + vendor_len
        count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        for _ in range(count):
            ln = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            entry = data[pos:pos + ln].decode("utf-8", "replace")
            pos += ln
            if "=" in entry:
                key, _, value = entry.partition("=")
                tags[key.upper()] = value
    except (struct.error, IndexError):
        pass
    return tags


def read_flac_info(path):
    """Return (streaminfo dict, tags dict)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            if head != b"fLaC":
                return {}, {}
            streaminfo = {}
            tags = {}
            while True:
                hdr = f.read(4)
                if len(hdr) < 4:
                    break
                last = bool(hdr[0] & 0x80)
                btype = hdr[0] & 0x7F
                size = int.from_bytes(hdr[1:4], "big")
                data = f.read(size)
                if btype == FLAC_STREAMINFO:
                    streaminfo = parse_flac_streaminfo(data)
                elif btype == FLAC_VORBIS_COMMENT:
                    tags = parse_vorbis_comments(data)
                if last or size <= 0:
                    break
            return streaminfo, tags
    except OSError:
        return {}, {}


# ---------- DSF ----------

DSD_RATES = {2822400: "DSD64", 5644800: "DSD128", 11289600: "DSD256", 22579200: "DSD512"}


def read_dsf_info(path):
    """Return (info dict, tags dict) for .dsf (DSD stream file)."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"DSD ":
                return {}, {}
            chunk_size = struct.unpack("<Q", f.read(8))[0]
            f.read(8)  # file size
            metadata_ptr = struct.unpack("<Q", f.read(8))[0]
            if f.read(4) != b"fmt ":
                return {}, {}
            fmt_size = struct.unpack("<Q", f.read(8))[0]
            fmt = f.read(fmt_size)
            if len(fmt) < 40:
                return {}, {}
            _ver, _fmtid, _chtype, channels, sample_rate, _bps, sample_count, _blk, _rsv = \
                struct.unpack("<IIIIIIQII", fmt[:40])
            tags = {}
            if metadata_ptr and 0 < metadata_ptr < os.path.getsize(path):
                f.seek(metadata_ptr)
                if f.read(3) == b"ID3":
                    f.seek(metadata_ptr)
                    tags = parse_id3v2(f.read(4 * 1024 * 1024))
            info = {
                "sample_rate": sample_rate,
                "channels": channels,
                "bits": 1,
                "total_samples": sample_count,
                "codec": DSD_RATES.get(sample_rate, "DSD"),
            }
            if sample_rate > 0:
                info["duration"] = sample_count / sample_rate
            return info, tags
    except (OSError, struct.error):
        return {}, {}


# ---------- DFF ----------

def read_dff_info(path):
    """Return (info dict, tags dict) for .dff (DSDIFF)."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"FRM8":
                return {}, {}
            f.read(8)  # total size
            if f.read(4) != b"DSD ":
                return {}, {}
            sample_rate = 0
            channels = 0
            while True:
                hdr = f.read(12)
                if len(hdr) < 12:
                    break
                cid, csize = hdr[0:4], struct.unpack(">Q", hdr[4:12])[0]
                if cid == b"PROP":
                    prop = f.read(csize)
                    pos = 4  # skip 'SND ' header
                    while pos + 12 <= len(prop):
                        pid = prop[pos:pos + 4]
                        psize = struct.unpack(">Q", prop[pos + 4:pos + 12])[0]
                        pdata = prop[pos + 12:pos + 12 + psize]
                        if pid == b"FS  " and psize >= 4:
                            sample_rate = struct.unpack(">I", pdata[:4])[0]
                        elif pid == b"CHNL" and psize >= 2:
                            channels = struct.unpack(">H", pdata[:2])[0]
                        pos += 12 + psize
                    break
                f.seek(csize, 1)
            info = {"sample_rate": sample_rate, "channels": channels, "bits": 1}
            if sample_rate:
                info["codec"] = DSD_RATES.get(sample_rate, "DSD")
                dsd_bytes = max(0, os.path.getsize(path) - f.tell())
                info["duration"] = dsd_bytes / (sample_rate * max(channels, 1) / 8)
            return info, {}
    except (OSError, struct.error):
        return {}, {}


# ---------- ID3v2 ----------

_ID3_FRAMES = {"TIT2": "title", "TPE1": "artist", "TALB": "album",
               "TCON": "genre", "TDRC": "year", "TYER": "year", "TORY": "year"}


def _decode_text(data):
    if not data:
        return ""
    enc = data[0]
    raw = data[1:]
    try:
        if enc == 0:
            return raw.decode("latin1", "replace").strip("\x00")
        if enc == 1:
            if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                return raw[2:].decode("utf-16", "replace").strip("\x00")
            return raw.decode("utf-16", "replace").strip("\x00")
        if enc == 2:
            return raw.decode("utf-16-be", "replace").strip("\x00")
        return raw.decode("utf-8", "replace").strip("\x00")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("latin1", "replace").strip("\x00")


def parse_id3v2(data):
    tags = {}
    if len(data) < 10 or data[:3] != b"ID3":
        return tags
    ver_major = data[3]
    flags = data[5]
    size = 0
    for b in data[6:10]:
        size = (size << 7) | (b & 0x7F)
    pos = 10
    if flags & 0x40:  # extended header
        if ver_major == 4:
            ext_size = 0
            for b in data[pos:pos + 4]:
                ext_size = (ext_size << 7) | (b & 0x7F)
            pos += ext_size
        else:
            ext_size = int.from_bytes(data[pos:pos + 4], "big")
            pos += 4 + ext_size
    end = min(10 + size, len(data))
    while pos + 10 <= end:
        fid = data[pos:pos + 4]
        if fid[0] == 0 or b"\x00\x00\x00\x00" == fid:
            break
        if ver_major == 4:
            fsize = 0
            for b in data[pos + 4:pos + 8]:
                fsize = (fsize << 7) | (b & 0x7F)
        else:
            fsize = int.from_bytes(data[pos + 4:pos + 8], "big")
        if fsize <= 0 or pos + 10 + fsize > end:
            break
        fdata = data[pos + 10:pos + 10 + fsize]
        key = _ID3_FRAMES.get(fid.decode("latin1"))
        if key and key not in tags:
            tags[key] = _decode_text(fdata)
        pos += 10 + fsize
    return tags


def read_tags_for(path):
    """Best-effort tag dict for any supported audio file."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        _, tags = read_flac_info(path)
        return tags
    if ext == ".dsf":
        _, tags = read_dsf_info(path)
        return tags
    if ext == ".dff":
        return {}
    if ext in (".mp3", ".aac", ".m4a"):
        try:
            with open(path, "rb") as f:
                return parse_id3v2(f.read(4 * 1024 * 1024))
        except OSError:
            return {}
    return {}
