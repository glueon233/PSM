"""HLS live-stream management via ffmpeg (optional; disabled when ffmpeg missing)."""

import hashlib
import os
import shutil
import subprocess
import threading
import time

from .util import find_ffmpeg

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

HLS_TIME = 4
HLS_LIST_SIZE = 8


class HlsManager:
    def __init__(self, source_dir, hls_dir):
        self.source_dir = source_dir
        self.hls_dir = hls_dir
        self.ffmpeg = find_ffmpeg()
        self._streams = {}
        self._lock = threading.Lock()

    @property
    def available(self):
        return bool(self.ffmpeg)

    def _out_dir(self, media_id):
        return os.path.join(self.hls_dir, hashlib.md5(media_id.encode("utf-8")).hexdigest())

    def _out_url(self, media_id):
        return "/hls/%s/index.m3u8" % hashlib.md5(media_id.encode("utf-8")).hexdigest()

    def status(self):
        with self._lock:
            return [
                {"id": mid, "url": s["url"], "state": s["state"], "started": s["started"], "error": s["error"]}
                for mid, s in sorted(self._streams.items(), key=lambda kv: kv[1]["started"])
            ]

    def start(self, source_path, media_id):
        if not self.ffmpeg:
            return None, "ffmpeg not found: HLS mode disabled"
        with self._lock:
            if media_id in self._streams and self._streams[media_id]["state"] == "running":
                return self._out_url(media_id), None
        out_dir = self._out_dir(media_id)
        shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir, exist_ok=True)
        playlist = os.path.join(out_dir, "index.m3u8")
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "warning",
            "-re", "-i", source_path,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-f", "hls", "-hls_time", str(HLS_TIME),
            "-hls_list_size", str(HLS_LIST_SIZE),
            "-hls_flags", "delete_segments+independent_segments",
            "-hls_playlist_type", "event",
            playlist,
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
        url = self._out_url(media_id)
        with self._lock:
            self._streams[media_id] = {
                "id": media_id, "url": url, "proc": proc, "state": "starting",
                "started": time.time(), "error": None, "stderr": [],
            }
        t = threading.Thread(target=self._watch, args=(media_id, proc), daemon=True)
        t.start()
        return url, None

    def stop(self, media_id):
        with self._lock:
            s = self._streams.get(media_id)
        if not s:
            return False
        proc = s["proc"]
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        with self._lock:
            self._streams.pop(media_id, None)
        shutil.rmtree(self._out_dir(media_id), ignore_errors=True)
        return True

    def _watch(self, media_id, proc):
        lines = []
        for raw in proc.stderr:
            try:
                lines.append(raw.decode("utf-8", "replace").strip())
            except Exception:
                pass
            if len(lines) > 12:
                lines.pop(0)
        rc = proc.wait()
        with self._lock:
            s = self._streams.get(media_id)
            if not s:
                return
            if rc == 0:
                s["state"] = "finished"
            else:
                s["state"] = "error"
                s["error"] = ("ffmpeg exit code %d" % rc) + ("\n" + "\n".join(lines[-8:]) if lines else "")

    def stop_all(self):
        for mid in list(self._streams.keys()):
            self.stop(mid)
