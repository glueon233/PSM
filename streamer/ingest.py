"""HTTP-TS ingest: MPEG-TS over HTTP POST publishing + live relay.

Publishers POST a continuous MPEG-TS byte stream to /ingest/<id>?key=…
The server demuxes 188-byte TS packets, keeps a rolling buffer aligned to
the latest PAT/random-access point, and relays the stream to viewers on
GET /live/<id>?token=… as a chunked TS feed.

Pure stdlib: packet parsing, join-point tracking and ring buffer need no
external dependencies.
"""

import collections
import re
import threading
import time

TS_PACKET = 188
SYNC_BYTE = 0x47
STREAM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")

BUFFER_MAX_PACKETS = 8192          # ~1.5 MB ≈ 6-12 s at 1-2 Mbps
BUFFER_MAX_AGE = 20.0              # seconds of history kept
STREAM_TIMEOUT = 15.0              # no data for this long -> offline
VIEWER_IDLE_TIMEOUT = 60.0         # viewer wait timeout while stream online


class _Packet:
    __slots__ = ("seq", "ts", "data")

    def __init__(self, seq, data):
        self.seq = seq
        self.ts = time.time()
        self.data = data


class TsStream:
    """Rolling TS packet buffer for one live stream."""

    def __init__(self, stream_id, publisher):
        self.stream_id = stream_id
        self.publisher = publisher
        self.started = time.time()
        self.bytes_in = 0
        self.dropped_packets = 0
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._buf = collections.deque()
        self._leftover = b""
        self._seq = 0
        self._last_pat = 0
        self._last_rai = 0
        self._viewers = 0
        self._abort = threading.Event()
        self._conn = None  # publisher socket (for instant kick)

    def set_conn(self, conn):
        self._conn = conn

    # ---------- publisher side ----------
    @property
    def last_data(self):
        with self._lock:
            return self._buf[-1].ts if self._buf else self.started

    @property
    def age(self):
        return time.time() - self.last_data

    @property
    def online(self):
        return self.age < STREAM_TIMEOUT

    @property
    def viewers(self):
        with self._lock:
            return self._viewers

    def abort(self):
        self._abort.set()
        conn = self._conn
        if conn is not None:
            try:
                import socket as _s
                conn.shutdown(_s.SHUT_RDWR)
            except OSError:
                pass

    @property
    def aborted(self):
        return self._abort.is_set()

    def push(self, data):
        """Feed raw bytes (may contain partial packets)."""
        with self._cond:
            self.bytes_in += len(data)
            data = self._leftover + data
            self._leftover = b""
            pos = 0
            n = len(data)
            while n - pos >= TS_PACKET:
                pkt = data[pos:pos + TS_PACKET]
                if pkt[0] == SYNC_BYTE:
                    self._add_packet(pkt)
                    pos += TS_PACKET
                else:  # desync: resync to next sync byte
                    self.dropped_packets += 1
                    rel = data.find(SYNC_BYTE, pos + 1, pos + TS_PACKET + 1)
                    if rel < 0:
                        pos += TS_PACKET
                    else:
                        pos = rel
            if pos < n:
                self._leftover = data[pos:]
            self._cond.notify_all()

    def _add_packet(self, pkt):
        self._seq += 1
        p = _Packet(self._seq, pkt)
        self._buf.append(p)
        if self._is_pat(pkt):
            self._last_pat = self._seq
        if self._is_rai(pkt):
            self._last_rai = self._seq
        now = p.ts
        while self._buf and (len(self._buf) > BUFFER_MAX_PACKETS
                             or now - self._buf[0].ts > BUFFER_MAX_AGE):
            self._buf.popleft()

    @staticmethod
    def _is_pat(pkt):
        # PAT: PID 0, payload_unit_start_indicator, payload table_id == 0x00
        if (pkt[1] & 0xE0) != 0x40:  # PUSI set, PID high bits 0
            return False
        if pkt[2] != 0x00:
            return False
        afc = (pkt[3] >> 4) & 0x3
        if afc != 1:  # payload only
            return False
        ptr = pkt[4]
        if ptr >= TS_PACKET - 5:
            return False
        return pkt[5 + ptr] == 0x00  # PSI table_id: PAT

    @staticmethod
    def _is_rai(pkt):
        afc = (pkt[3] >> 4) & 0x3
        if afc in (0, 1):  # no adaptation field
            return False
        if pkt[4] < 1:
            return False
        return bool(pkt[5] & 0x40)  # random_access_indicator

    # ---------- viewer side ----------
    def register_viewer(self):
        with self._lock:
            self._viewers += 1

    def unregister_viewer(self):
        with self._lock:
            self._viewers = max(0, self._viewers - 1)

    def snapshot(self):
        """Return (seq, bytes) from the join point (latest PAT/RAI)."""
        with self._lock:
            join = self._last_rai if self._last_rai > self._last_pat else self._last_pat
            data = b"".join(p.data for p in self._buf if p.seq >= join)
            return join, data

    def wait_more(self, after_seq, timeout=VIEWER_IDLE_TIMEOUT):
        """Block until packets newer than after_seq exist.

        Returns (bytes, latest_seq) or (None, after_seq) when the stream
        went offline while waiting.
        """
        deadline = time.time() + timeout
        with self._cond:
            while self._seq <= after_seq:
                if not self.online or self._abort.is_set():
                    return None, after_seq
                remain = deadline - time.time()
                if remain <= 0:
                    return b"", self._seq
                self._cond.wait(timeout=min(remain, 2.0))
            if not self._buf:
                return b"", after_seq
            data = b"".join(p.data for p in self._buf if p.seq > after_seq)
            return data, self._seq


class IngestManager:
    """Registry of live streams; enforces one publisher per stream id."""

    def __init__(self):
        self._lock = threading.RLock()
        self._streams = {}

    def acquire(self, stream_id, publisher):
        """Create and lock a stream for `publisher`. Returns (stream, err)."""
        if not STREAM_ID_RE.match(stream_id):
            return None, "流名称需 3-64 位，仅限字母/数字/_-"
        with self._lock:
            st = self._streams.get(stream_id)
            if st is not None and st.online:
                return None, "该流已被占用（推流中）"
            st = TsStream(stream_id, publisher)
            self._streams[stream_id] = st
            return st, None

    def release(self, stream_id):
        with self._lock:
            self._streams.pop(stream_id, None)

    def get(self, stream_id):
        with self._lock:
            return self._streams.get(stream_id)

    def list_online(self):
        with self._lock:
            out = []
            for st in self._streams.values():
                out.append({
                    "id": st.stream_id,
                    "publisher": st.publisher,
                    "started": st.started,
                    "online": st.online,
                    "age": round(st.age, 1),
                    "bytes_in": st.bytes_in,
                    "viewers": st.viewers,
                    "dropped": st.dropped_packets,
                })
        out.sort(key=lambda x: x["started"], reverse=True)
        return out

    def kick(self, stream_id):
        st = self.get(stream_id)
        if not st:
            return False
        st.abort()
        return True

    def stop_all(self):
        for st in list(self._streams.values()):
            st.abort()
