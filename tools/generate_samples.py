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


if __name__ == "__main__":
    sys.exit(main())
