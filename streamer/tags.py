"""Hi-res audio metadata: FLAC streaminfo + Vorbis comments, DSF/DFF headers, ID3v2.

Pure stdlib parsers used for probing duration/bitrate and DIDL tag enrichment
(artist/album/genre/…) so AIMP can display full hi-fi metadata.
"""

import os
import re
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


# ---------- subtitles ----------

LANG_MAP = {
    "zh-cn": "简体中文", "chs": "简体中文", "sc": "简体中文", "zh-hans": "简体中文",
    "zh-tw": "繁體中文", "cht": "繁體中文", "tc": "繁體中文", "zh-hant": "繁體中文",
    "zh": "中文", "en": "English", "ja": "日本語", "jp": "日本語",
    "ko": "한국어", "fr": "Français", "de": "Deutsch", "es": "Español",
    "ru": "Русский",
}


def guess_language_from_name(name):
    stem = os.path.splitext(name)[0]
    parts = stem.split(".")
    if len(parts) >= 2:
        key = parts[-1].lower()
        return LANG_MAP.get(key)
    return None


def _ass_time_to_seconds(t):
    # H:MM:SS.cc
    try:
        h, rest = t.split(":", 1)
        m, rest2 = rest.split(":", 1)
        s, c = rest2.split(".", 1)
        return int(h) * 3600 + int(m) * 60 + int(s) + int(c.ljust(2, "0")[:2]) / 100.0
    except (ValueError, AttributeError):
        return None


def read_subtitle_info(path):
    """Return (info dict, tags dict) for .ass/.srt subtitle files."""
    ext = os.path.splitext(path)[1].lower()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        try:
            with open(path, "r", encoding="gb18030", errors="replace") as f:
                text = f.read()
        except OSError:
            return {}, {}
    if ext == ".ass":
        return _ass_info(text)
    if ext == ".srt":
        return _srt_info(text)
    return {}, {}


def _ass_info(text):
    info = {}
    tags = {}
    max_end = 0.0
    in_events = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_events = stripped.lower().startswith("[events]")
            continue
        if not in_events:
            m = re.match(r"^(Title|Original Script|PlayResX|PlayResY)\s*:\s*(.*)$", stripped)
            if m:
                tags[m.group(1).lower().replace(" ", "_")] = m.group(2).strip()
            continue
        if stripped.lower().startswith("dialogue:"):
            try:
                _layer, start, end = stripped.split(",", 3)[:3]
                end_s = _ass_time_to_seconds(end.strip())
                if end_s:
                    max_end = max(max_end, end_s)
            except (ValueError, IndexError):
                continue
    if max_end:
        info["duration"] = max_end
    info["codec"] = "ASS 字幕"
    return info, tags


def _srt_info(text):
    info = {"codec": "SRT 字幕"}
    max_end = 0.0
    for line in text.splitlines():
        m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})", line)
        if m:
            end = (int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7))
                   + int(m.group(8).ljust(3, "0")) / 1000.0)
            max_end = max(max_end, end)
    if max_end:
        info["duration"] = max_end
    return info, {}

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


# ---------- Matroska / EBML ----------

CODEC_MAP = {
    "V_MPEG4/ISO/AVC": "h264", "V_MPEGH/ISO/HEVC": "hevc",
    "V_MPEG4/ISO/ASP": "mpeg4", "V_MPEG2": "mpeg2", "V_MPEG1": "mpeg1",
    "V_VP8": "vp8", "V_VP9": "vp9", "V_AV1": "av1",
    "V_THEORA": "theora", "V_MS/VFW/FOURCC": "msvc",
    "A_FLAC": "FLAC", "A_AAC": "AAC", "A_MPEG/L3": "MP3",
    "A_MPEG/L2": "MP2", "A_AC3": "AC3", "A_EAC3": "EAC3",
    "A_DTS": "DTS", "A_OPUS": "Opus", "A_VORBIS": "Vorbis",
    "A_PCM/INT/LIT": "PCM", "A_PCM/FLOAT/IEEE": "PCMf",
    "A_TRUEHD": "TrueHD", "A_MLP": "MLP", "A_ALAC": "ALAC",
    "A_WAVPACK4": "WavPack", "A_APE": "APE", "A_TTA1": "TTA",
    "S_TEXT/UTF8": "SRT", "S_TEXT/ASS": "ASS", "S_TEXT/SSA": "SSA",
    "S_VOBSUB": "VobSub", "S_HDMV/PGS": "PGS",
}


def _ebml_read_id(f):
    b = f.read(1)
    if not b:
        return None, 0
    first = b[0]
    if first == 0:
        return None, 0
    length = 1
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
    eid = first & (mask - 1)
    for _ in range(length - 1):
        nxt = f.read(1)
        if not nxt:
            return None, 0
        eid = (eid << 8) | nxt[0]
    return eid, length


def _ebml_read_size(f):
    b = f.read(1)
    if not b:
        return None, 0
    first = b[0]
    if first == 0:
        return None, 0
    length = 1
    mask = 0x80
    while not (first & mask):
        mask >>= 1
        length += 1
    size = first & (mask - 1)
    for _ in range(length - 1):
        nxt = f.read(1)
        if not nxt:
            return None, 0
        size = (size << 8) | nxt[0]
    # unknown size: every value bit set (all-ones payload)
    if size == (1 << (7 * (length - 1))) - 1:
        return None, length
    return size, length


def _ebml_read_uint(data):
    v = 0
    for b in data:
        v = (v << 8) | b
    return v


def read_mkv_info(path):
    """Parse Matroska segment: duration, tracks (codecs), resolution."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"\x1a\x45\xdf\xa3":
                return {}, {}
            # skip the EBML header element (its size follows the magic)
            size, slen = _ebml_read_size(f)
            if size is None:
                return {}, {}
            f.seek(size, 1)
            # walk top-level: find Segment (0x08538067)
            while True:
                eid, _ = _ebml_read_id(f)
                if eid is None:
                    return {}, {}
                size, _ = _ebml_read_size(f)
                if eid == 0x08538067:
                    return _parse_segment(f, size)
                if size is None:
                    return {}, {}
                f.seek(size, 1)
    except OSError:
        return {}, {}


def _parse_segment(f, size):
    info = {}
    timestamp_scale = 1000000.0
    duration = None
    seg_end = None
    if size is not None:
        seg_end = f.tell() + size
    while True:
        if seg_end is not None and f.tell() >= seg_end - 1:
            break
        eid, idlen = _ebml_read_id(f)
        if eid is None or idlen == 0:
            break
        esize, slen = _ebml_read_size(f)
        if esize is None:
            break
        if eid == 0x0549A966:  # Info
            ts, dur = _parse_info(f, esize)
            if ts:
                timestamp_scale = ts
            if dur is not None:
                duration = dur
        elif eid == 0x0654AE6B:  # Tracks
            _parse_tracks(f, esize, info)
        else:
            f.seek(esize, 1)
    if duration is not None:
        info["duration"] = duration * timestamp_scale / 1e9
    return info, {}


def _parse_info(f, size):
    end = f.tell() + size
    timestamp_scale = None
    duration = None
    while f.tell() < end - 1:
        eid, idlen = _ebml_read_id(f)
        if eid is None or idlen == 0:
            break
        esize, _ = _ebml_read_size(f)
        if esize is None:
            break
        data = f.read(esize) if esize <= 16 else None
        if data is None:
            f.seek(esize, 1)
            continue
        if eid == 0x0AD7B1 and len(data) <= 8:  # TimestampScale
            timestamp_scale = _ebml_read_uint(data)
        elif eid == 0x0489 and len(data) in (4, 8):  # Duration (float)
            duration = struct.unpack(">d" if len(data) == 8 else ">f", data)[0]
    return timestamp_scale, duration


def _parse_tracks(f, size, info):
    end = f.tell() + size
    while f.tell() < end - 1:
        eid, idlen = _ebml_read_id(f)
        if eid is None or idlen == 0:
            break
        esize, _ = _ebml_read_size(f)
        if esize is None:
            break
        if eid == 0x2E:  # TrackEntry
            _parse_track_entry(f, esize, info)
        else:
            f.seek(esize, 1)


def _parse_track_entry(f, size, info):
    end = f.tell() + size
    track_type = None
    codec_id = None
    width = height = None
    sample_rate = channels = None
    while f.tell() < end - 1:
        eid, idlen = _ebml_read_id(f)
        if eid is None or idlen == 0:
            break
        esize, _ = _ebml_read_size(f)
        if esize is None:
            break
        if eid == 0x03 and esize <= 4:  # TrackType
            track_type = _ebml_read_uint(f.read(esize))
        elif eid == 0x06:  # CodecID
            codec_id = f.read(esize).decode("latin1", "replace").strip("\x00")
        elif eid == 0x60:  # Video
            width, height = _parse_video(f, esize)
        elif eid == 0x61:  # Audio
            sample_rate, channels = _parse_audio(f, esize)
        else:
            f.seek(esize, 1)
    if track_type == 1 and codec_id:  # video
        info.setdefault("video_codec", CODEC_MAP.get(codec_id, codec_id))
        if width:
            info["width"] = width
        if height:
            info["height"] = height
    elif track_type == 2 and codec_id:  # audio
        info.setdefault("audio_codec", CODEC_MAP.get(codec_id, codec_id))
        if sample_rate:
            info.setdefault("sample_rate", int(sample_rate))
        if channels:
            info.setdefault("channels", channels)
    elif track_type == 17 and codec_id and "sub_codec" not in info:
        info["sub_codec"] = CODEC_MAP.get(codec_id, codec_id)


def _parse_video(f, size):
    end = f.tell() + size
    width = height = None
    while f.tell() < end - 1:
        eid, idlen = _ebml_read_id(f)
        if eid is None or idlen == 0:
            break
        esize, _ = _ebml_read_size(f)
        if esize is None:
            break
        if eid == 0x30 and esize <= 4:  # PixelWidth
            width = _ebml_read_uint(f.read(esize))
        elif eid == 0x3A and esize <= 4:  # PixelHeight
            height = _ebml_read_uint(f.read(esize))
        else:
            f.seek(esize, 1)
    return width, height


def _parse_audio(f, size):
    end = f.tell() + size
    sample_rate = channels = None
    while f.tell() < end - 1:
        eid, idlen = _ebml_read_id(f)
        if eid is None or idlen == 0:
            break
        esize, _ = _ebml_read_size(f)
        if esize is None:
            break
        if eid == 0x35 and esize <= 8:  # SamplingFrequency (float)
            data = f.read(esize)
            if len(data) == 8:
                sample_rate = struct.unpack(">d", data)[0]
            elif len(data) == 4:
                sample_rate = struct.unpack(">f", data)[0]
            else:
                sample_rate = _ebml_read_uint(data)
        elif eid == 0x1F and esize <= 4:  # Channels
            channels = _ebml_read_uint(f.read(esize))
        else:
            f.seek(esize, 1)
    return sample_rate, channels


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
