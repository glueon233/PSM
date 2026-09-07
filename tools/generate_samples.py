#!/usr/bin/env python3
"""Generate simulated media sources into ../source (pure stdlib, no ffmpeg).

Outputs:
  sample_01_color_bars.avi  animated SMPTE-style color bars (DIB / BI_RGB)
  sample_02_gradient.avi    moving gradient + frame counter
  sample_03_tone_440.wav    440Hz sine tone (PCM 16-bit mono)

Uncompressed DIB frames keep the AVI 100% playable in VLC and can be
generated with nothing but struct/math.
"""

import math
import os
import struct
import sys
import wave

W, H = 160, 120
FPS = 10
SECONDS = 5
NFRAMES = FPS * SECONDS
ROW_BYTES = W * 3          # 24-bit, already 4-byte aligned
FRAME_BYTES = ROW_BYTES * H

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "source")


def riff_chunk(cid, payload):
    data = payload if isinstance(payload, bytes) else b"".join(payload)
    pad = (-len(data)) % 2
    return cid + struct.pack("<I", len(data)) + data + b"\x00" * pad


def riff_list(list_type, payload):
    body = list_type + b"".join(payload)
    pad = (-len(body)) % 2
    return b"LIST" + struct.pack("<I", len(body)) + body + b"\x00" * pad


def frame_to_bytes(framebuf):
    """framebuf: list of rows, each row = bytes(BGR BGR ...), top-down."""
    return b"".join(framebuf)


def write_avi(path, frame_gen):
    # --- main AVI header (56 bytes) ---
    avih = struct.pack(
        "<IIIIIIIIIIIIII",
        1_000_000 // FPS,   # dwMicroSecPerFrame
        FRAME_BYTES * FPS,  # dwMaxBytesPerSec
        0,                  # dwPaddingGranularity
        0x10,               # dwFlags (AVIF_HASINDEX)
        NFRAMES,            # dwTotalFrames
        0,                  # dwInitialFrames
        1,                  # dwStreams
        FRAME_BYTES,        # dwSuggestedBufferSize
        W, H,               # dwWidth, dwHeight
        0, 0, 0, 0,         # dwReserved
    )
    # --- stream header 'strh' (56 bytes) ---
    strh = struct.pack(
        "<4s4sIHHIIIIIIIIHHHH",
        b"vids", b"DIB ", 0, 0, 0, 0,
        1, FPS, 0, NFRAMES, FRAME_BYTES, 0xFFFFFFFF, 0,
        0, 0, W, H,
    )
    # --- stream format 'strf' BITMAPINFOHEADER (40 bytes) ---
    strf = struct.pack(
        "<IIIHHIIIIII",
        40, W, H, 1, 24, 0, FRAME_BYTES, 0, 0, 0, 0,
    )

    movi = []
    for f in frame_gen():
        body = frame_to_bytes(f)
        movi.append(riff_chunk(b"00dc", body))

    hdrl = riff_list(b"hdrl", [riff_chunk(b"avih", avih),
                               riff_list(b"strl", [riff_chunk(b"strh", strh),
                                                   riff_chunk(b"strf", strf)])])
    movi_chunk = riff_list(b"movi", movi)

    with open(path, "wb") as fh:
        fh.write(b"RIFF")
        fh.write(struct.pack("<I", 4 + len(hdrl) + len(movi_chunk)))
        fh.write(b"AVI ")
        fh.write(hdrl)
        fh.write(movi_chunk)


def gen_color_bars():
    # SMPTE-like: gray, yellow, cyan, green, magenta, red, blue (7 bars)
    bars = [
        (191, 191, 191), (191, 191, 0), (0, 191, 191), (0, 191, 0),
        (191, 0, 191), (191, 0, 0), (0, 0, 191),
    ]
    bar_w = W // len(bars)

    for n in range(NFRAMES):
        offset = (n * 4) % bar_w
        rows = []
        for y in range(H):
            row = bytearray()
            if y > H - 16:  # moving bottom strip (shows motion)
                c = bars[(y + offset) % len(bars)]
                row += bytes(c) * W
            else:
                for x in range(W):
                    c = bars[min(x // bar_w, len(bars) - 1)]
                    row += bytes(c)
            rows.append(bytes(row))
        yield rows


def gen_gradient():
    cx = cy = 0
    for n in range(NFRAMES):
        cx = (n * 6) % (W * 2) - W // 2
        cy = int(H / 2 + H / 2 * math.sin(n / NFRAMES * 2 * math.pi * 2))
        rows = []
        for y in range(H):
            row = bytearray()
            for x in range(W):
                d = math.sqrt((x - cx - W / 2) ** 2 + (y - cy) ** 2) / 140.0
                r = int(255 * (0.5 + 0.5 * math.sin(d * 6 + n * 0.3)))
                g = int(255 * (0.5 + 0.5 * math.sin(d * 6 + 2.1 + n * 0.3)))
                b = int(255 * (0.5 + 0.5 * math.sin(d * 6 + 4.2 + n * 0.3)))
                row += bytes((max(0, min(255, b)), max(0, min(255, g)), max(0, min(255, r))))
            rows.append(bytes(row))
        yield rows


def write_wav(path, freq=440.0, seconds=5, rate=44100):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            env = min(1.0, i / (rate * 0.05), (rate * seconds - i) / (rate * 0.05))
            v = int(20000 * env * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))


def flac_crc8(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def flac_crc16(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def utf8_encode_number(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x800:
        return bytes([0xC0 | (n >> 6), 0x80 | (n & 0x3F)])
    if n < 0x10000:
        return bytes([0xE0 | (n >> 12), 0x80 | ((n >> 6) & 0x3F), 0x80 | (n & 0x3F)])
    return bytes([0xF0 | (n >> 18), 0x80 | ((n >> 12) & 0x3F),
                  0x80 | ((n >> 6) & 0x3F), 0x80 | (n & 0x3F)])


def flac_block_header(btype, last, size):
    return bytes([(0x80 if last else 0) | btype]) + size.to_bytes(3, "big")


def flac_streaminfo_block(sample_rate, channels, bps, total_samples, block_size=4096):
    min_frame = max_frame = 16
    bits = ((block_size << 128) | (block_size << 112)
            | (min_frame << 88) | (max_frame << 64)
            | (sample_rate << 44) | ((channels - 1) << 41) | ((bps - 1) << 36)
            | total_samples)
    return bits.to_bytes(18, "big") + b"\x00" * 16


def flac_vorbis_block(entries):
    payload = b"StreamMedia sample generator".ljust(8, b"\x00")[:8]
    # vendor
    payload = struct.pack("<I", 8) + b"StreamMd" + struct.pack("<I", len(entries))
    for k, v in entries:
        e = ("%s=%s" % (k, v)).encode("utf-8")
        payload += struct.pack("<I", len(e)) + e
    return payload


def write_flac(path, sample_rate=44100, channels=1, bps=16, seconds=10,
               block_size=4096, tags=None):
    """Constant-subframe FLAC encoder (pure stdlib). The DC value of each
    block follows a sine wave, producing an audible stepped tone."""
    total = sample_rate * seconds
    nblocks = (total + block_size - 1) // block_size

    frames = bytearray()
    for i in range(nblocks):
        t = (i * block_size + block_size / 2) / total
        value = int(10000 * math.sin(2 * math.pi * 8 * t))
        frame_no = i * block_size
        header = ((0x3FFE << 18) | (12 << 12) | (9 << 8) | (0 << 4) | (4 << 1)
                  ).to_bytes(4, "big") + utf8_encode_number(frame_no)
        subframe = b"\x00" + value.to_bytes(bps // 8, "big", signed=True)
        frames += header + bytes([flac_crc8(header)]) + subframe
        frames += flac_crc16(subframe).to_bytes(2, "big")

    with open(path, "wb") as f:
        f.write(b"fLaC")
        f.write(flac_block_header(0, False, 34) + flac_streaminfo_block(
            sample_rate, channels, bps, total, block_size))
        vc = flac_vorbis_block(list((tags or {}).items()))
        f.write(flac_block_header(4, True, len(vc)) + vc)
        f.write(bytes(frames))


def dsf_data_blocks(seconds, sample_rate, channels, block_size=4096):
    """DSD silence: alternating 0x69/0x96 patterns per block header."""
    total_bytes = sample_rate * seconds * channels // 8
    blocks = bytearray()
    nblocks = (total_bytes + block_size - 1) // block_size
    for i in range(nblocks):
        if i % 2 == 0:
            blocks += b"\x05\xfa"
        else:
            blocks += b"\xfa\x05"
        rem = min(block_size, total_bytes - i * block_size)
        blocks += b"\x69" * rem
    return bytes(blocks)


def id3v2_23(frames):
    body = bytearray()
    for fid, text in frames:
        payload = b"\x03" + text.encode("utf-8")  # encoding 3 = UTF-8
        body += fid.encode("latin1") + struct.pack(">I", len(payload)) + b"\x00\x00" + payload
    size = len(body)
    synchsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                       (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x03\x00\x00" + synchsafe + bytes(body)


def write_dsf(path, sample_rate=2822400, channels=2, seconds=10, tags=None):
    data = dsf_data_blocks(seconds, sample_rate, channels)
    fmt = struct.pack("<IIIIIIQII", 1, 0, 2 if channels == 2 else 1, channels,
                      sample_rate, 1, sample_rate * seconds, 4096, 0)
    fmt_chunk = b"fmt " + struct.pack("<Q", len(fmt)) + fmt
    data_chunk = b"data" + struct.pack("<Q", len(data)) + data
    head_size = 28 + len(fmt_chunk) + len(data_chunk)
    meta = id3v2_23(list((tags or {}).items()) + [("TIT2", "DSD64 Test")]) if tags is None else \
        id3v2_23([("TIT2", tags.get("TITLE", "DSD64 Test")),
                  ("TPE1", tags.get("ARTIST", "StreamMedia")),
                  ("TALB", tags.get("ALBUM", "Test Samples")),
                  ("TCON", tags.get("GENRE", "Test"))])
    file_size = head_size + len(meta)
    with open(path, "wb") as f:
        f.write(b"DSD ")
        f.write(struct.pack("<Q", file_size - 12))
        f.write(struct.pack("<Q", file_size))
        f.write(struct.pack("<Q", head_size))
        f.write(fmt_chunk)
        f.write(data_chunk)
        f.write(meta)


def dff_dsd_data(seconds, sample_rate, channels):
    total = sample_rate * seconds * channels // 8
    return b"\x69" * total


def write_dff(path, sample_rate=2822400, channels=2, seconds=10, tags=None):
    data = dff_dsd_data(seconds, sample_rate, channels)
    prop = (b"SND "
            + b"FS  " + struct.pack(">Q", 4) + struct.pack(">I", sample_rate)
            + b"CHNL" + struct.pack(">Q", 2 + 4 * channels)
            + struct.pack(">H", channels) + b"SLFT" + b"SRGT"
            + b"CMPR" + struct.pack(">Q", 4 + 4 + 14) + b"DSD " + b"not compressed\x00")
    fver = b"FVER" + struct.pack(">Q", 4) + struct.pack(">I", 0x01050000)
    prop_chunk = b"PROP" + struct.pack(">Q", len(prop)) + prop
    dsd_chunk = b"DSD " + struct.pack(">Q", len(data)) + data
    body = (b"FRM8" + struct.pack(">Q", 0) + b"DSD " + fver + prop_chunk + dsd_chunk)
    meta = id3v2_23([("TIT2", tags.get("TITLE", "DSD64 Test") if tags else "DSD64 Test"),
                     ("TPE1", tags.get("ARTIST", "StreamMedia") if tags else "StreamMedia"),
                     ("TALB", tags.get("ALBUM", "Test Samples") if tags else "Test Samples"),
                     ("TCON", tags.get("GENRE", "Test") if tags else "Test")])
    total_size = len(body) - 12 + len(meta)
    with open(path, "wb") as f:
        f.write(body[:4])
        f.write(struct.pack(">Q", total_size))
        f.write(body[12:])
        f.write(meta)


ASS_TEMPLATE = """[Script Info]
; Generated by StreamMedia sample generator
Title: {title}
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,36,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,0,2,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
{events}
"""

SRT_TEMPLATE = """{i}
00:00:{s1:02d},{m1:03d} --> 00:00:{s2:02d},{m2:03d}
{text}

"""


def write_ass(path, title="Test Captions", lines=None, seconds=10):
    lines = lines or [
        (0.5, 2.5, "{\\fad(200,200)}StreamMedia 字幕测试"),
        (3.0, 6.0, "这是一条 ASS 字幕"),
        (6.5, 9.5, "支持样式与特效"),
    ]

    def fmt(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        c = int(round((t - int(t)) * 100))
        return "%d:%02d:%02d.%02d" % (h, m, s, c)

    events = "\n".join(
        "Dialogue: 0,%s,%s,Default,,0,0,0,,%s" % (fmt(a), fmt(b), txt)
        for a, b, txt in lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write(ASS_TEMPLATE.format(title=title, events=events))


def write_srt(path, lines=None, seconds=10):
    lines = lines or [
        (0.5, 2.5, "SRT subtitle test"),
        (3.0, 6.0, "second cue"),
        (6.5, 9.5, "last cue"),
    ]
    out = []
    for i, (a, b, txt) in enumerate(lines, 1):
        s1, m1 = divmod(int(a * 1000), 1000)
        s2, m2 = divmod(int(b * 1000), 1000)
        out.append(SRT_TEMPLATE.format(i=i, s1=s1, m1=m1, s2=s2, m2=m2, text=txt))
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(out))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    paths = [
        (os.path.join(OUT_DIR, "sample_01_color_bars.avi"), gen_color_bars),
        (os.path.join(OUT_DIR, "sample_02_gradient.avi"), gen_gradient),
    ]
    for path, gen in paths:
        write_avi(path, gen)
        print("generated: %s (%.1f MB)" % (path, os.path.getsize(path) / 1e6))
    wav_path = os.path.join(OUT_DIR, "sample_03_tone_440.wav")
    write_wav(wav_path)
    print("generated: %s (%.1f MB)" % (wav_path, os.path.getsize(wav_path) / 1e6))

    flac_tags = {"TITLE": "Stepped Sine Test", "ARTIST": "StreamMedia",
                 "ALBUM": "Test Samples", "GENRE": "Test", "DATE": "2026"}
    flac_path = os.path.join(OUT_DIR, "sample_04_hi_res.flac")
    write_flac(flac_path, sample_rate=96000, channels=1, bps=16, seconds=10, tags=flac_tags)
    print("generated: %s (%.1f MB)" % (flac_path, os.path.getsize(flac_path) / 1e6))

    dsf_path = os.path.join(OUT_DIR, "sample_05_dsd64.dsf")
    write_dsf(dsf_path, tags={"TITLE": "DSD64 Silence Test"})
    print("generated: %s (%.1f MB)" % (dsf_path, os.path.getsize(dsf_path) / 1e6))

    dff_path = os.path.join(OUT_DIR, "sample_06_dsd64.dff")
    write_dff(dff_path, tags={"TITLE": "DSD64 DFF Test"})
    print("generated: %s (%.1f MB)" % (dff_path, os.path.getsize(dff_path) / 1e6))

    ass_path = os.path.join(OUT_DIR, "sample_01_color_bars.zh-cn.ass")
    write_ass(ass_path, title="Color Bars 中文字幕")
    print("generated: %s (%.1f KB)" % (ass_path, os.path.getsize(ass_path) / 1e3))

    srt_path = os.path.join(OUT_DIR, "sample_02_gradient.en.srt")
    write_srt(srt_path)
    print("generated: %s (%.1f KB)" % (srt_path, os.path.getsize(srt_path) / 1e3))

    ass2_path = os.path.join(OUT_DIR, "sample_07_standalone.ass")
    write_ass(ass2_path, title="Standalone ASS")
    print("generated: %s (%.1f KB)" % (ass2_path, os.path.getsize(ass2_path) / 1e3))


if __name__ == "__main__":
    sys.exit(main())
