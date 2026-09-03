"""HTTP application: management page, REST API, Range-based media streaming."""

import json
import os
import re
import socket
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .hls import HlsManager
from .ingest import IngestManager, STREAM_ID_RE
from .library import probe, resolve_source, scan_library
from .store import Store, StoreError
from .util import guess_mime, quote_relpath

CHUNK = 1024 * 256
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

MIME_HTML = "text/html; charset=utf-8"
MIME_JSON = "application/json; charset=utf-8"

COOKIE_CLIENT = "client_session"
COOKIE_ADMIN = "admin_session"


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class StreamingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, source_dir, hls_dir, static_dir,
                 client_dir, data_dir):
        super().__init__(addr, handler)
        self.source_dir = source_dir
        self.hls_dir = hls_dir
        self.static_dir = static_dir
        self.client_dir = client_dir
        self.store = Store(os.path.join(data_dir, "db.json"))
        self.hls = HlsManager(source_dir, hls_dir)
        self.ingest = IngestManager()
        self.dlna = None
        self.start_time = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "StreamMedia/1.0 UPnP/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- helpers ----------
    @property
    def srv(self):
        return self.server

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", MIME_JSON)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, message):
        self._json({"error": message}, code)

    def _json_cookie(self, obj, code, cookie_name, value, max_age):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", MIME_JSON)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._set_cookie(cookie_name, value, max_age)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_048_576:
            raise HttpError(400, "bad body")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # ---------- cookies / sessions ----------
    def _cookies(self):
        out = {}
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                out[k.strip()] = urllib.parse.unquote(v.strip())
        return out

    def _set_cookie(self, name, value, max_age):
        self.send_header("Set-Cookie", "%s=%s; Max-Age=%d; Path=/; HttpOnly; SameSite=Lax"
                         % (name, value, max_age))

    def _session(self, cookie_name):
        token = self._cookies().get(cookie_name)
        if not token:
            return None
        return self.srv.store.get_session(token)

    def _client(self):
        s = self._session(COOKIE_CLIENT)
        if not s or s["kind"] != "client":
            return None
        u = self.srv.store.get_user(s["user"])
        if not u or u["status"] != "active":
            return None
        return u

    def _admin(self):
        s = self._session(COOKIE_ADMIN)
        return bool(s and s["kind"] == "admin")

    def _require_client(self):
        u = self._client()
        if not u:
            raise HttpError(401, "请先登录（账号需经管理员审批）")
        return u

    def _require_admin(self):
        if not self._admin():
            raise HttpError(401, "需要管理员登录")
        return True

    def _send_file(self, path, mime, range_header=None, attach=False):
        size = os.path.getsize(path)
        start, end = 0, size - 1
        status = 200

        if range_header:
            try:
                start, end = self._parse_range(range_header, size)
                status = 206
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        if attach:
            self.send_header("Content-Disposition",
                             "attachment; filename=\"%s\"" % os.path.basename(path))
        self.end_headers()

        with open(path, "rb") as f:
            f.seek(start)
            remain = length
            while remain > 0:
                buf = f.read(min(CHUNK, remain))
                if not buf:
                    break
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remain -= len(buf)

    @staticmethod
    def _parse_range(header, size):
        if size <= 0:
            raise ValueError("empty file")
        m = RANGE_RE.match(header.strip())
        if not m:
            raise ValueError("bad range")
        a, b = m.groups()
        if not a and not b:
            raise ValueError("no range")
        if not a:
            suffix = int(b)
            if suffix == 0:
                raise ValueError("bad suffix")
            start = max(size - suffix, 0)
            end = size - 1
        else:
            start = int(a)
            end = int(b) if b else size - 1
        if start >= size or start > end:
            raise ValueError("unsatisfiable")
        return start, min(end, size - 1)

    # ---------- routing ----------
    def do_GET(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            if path == "/":
                self.send_response(302)
                self.send_header("Location", "/client/")
                self.end_headers()
                return
            if path.startswith("/client/"):
                return self._client_static(path[len("/client/"):])
            if path in ("/admin", "/admin/"):
                return self._send_file(os.path.join(self.srv.static_dir, "index.html"), MIME_HTML)
            if path == "/admin/login.html":
                return self._send_file(os.path.join(self.srv.static_dir, "login.html"), MIME_HTML)
            if path.startswith("/static/"):
                return self._static(path)
            if path == "/api/auth/me":
                u = self._client()
                return self._json({"user": u or None, "admin": self._admin()})
            if path == "/api/videos":
                if not self._admin():
                    self._require_client()
                return self._json({"items": scan_library(self.srv.source_dir)})
            if path == "/api/links":
                if self._admin():
                    items = self.srv.store.list_links(None)
                else:
                    u = self._require_client()
                    items = self.srv.store.list_links(u["username"])
                return self._json({"items": items})
            if path == "/api/stream-keys":
                u = self._require_client()
                items = self.srv.store.list_stream_keys(
                    None if self._admin() else u["username"])
                return self._json({"items": items})
            if path == "/api/live":
                if not self._admin():
                    self._require_client()
                return self._json({"items": self.srv.ingest.list_online()})
            if path == "/api/admin/users":
                self._require_admin()
                return self._json({"items": self.srv.store.list_users()})
            if path == "/api/admin/links":
                self._require_admin()
                return self._json({"items": self.srv.store.list_links()})
            if path == "/api/status":
                return self._json({
                    "ffmpeg": self.srv.hls.ffmpeg,
                    "hls_available": self.srv.hls.available,
                    "uptime": round(time.time() - self.srv.start_time),
                    "dlna": self.srv.dlna.status() if self.srv.dlna else {"enabled": False},
                })
            if path == "/api/streams":
                self._require_admin()
                return self._json({"items": self.srv.hls.status()})
            if path.startswith("/play/"):
                return self._play(path[len("/play/"):], parsed.query, require_auth=True)
            if path.startswith("/MediaItems/"):
                return self._play(path[len("/MediaItems/"):], parsed.query, require_auth=False)
            if path.startswith("/live/"):
                return self._live(path[len("/live/"):], parsed.query)
            if path.startswith("/hls/"):
                return self._hls_file(path[len("/hls/"):])
            if path == "/rootDesc.xml" and self.srv.dlna:
                xml = self.srv.dlna.device_xml(self.headers.get("Host"))
                body = xml.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/upnp/scpd/") and self.srv.dlna:
                svc = os.path.basename(path)[:-4]
                xml = self.srv.dlna.scpd(svc)
                if xml is None:
                    raise HttpError(404, "unknown scpd")
                body = xml.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/probe":
                self._require_client()
                mid = urllib.parse.parse_qs(parsed.query).get("id", [None])[0]
                if not mid:
                    raise HttpError(400, "missing id")
                full = resolve_source(self.srv.source_dir, mid)
                if not full:
                    raise HttpError(404, "unknown media")
                return self._json(probe(full))
            raise HttpError(404, "not found")
        except HttpError as e:
            self._err(e.code, e.message)
        except Exception:
            import traceback
            traceback.print_exc()
            self._err(500, "internal error")

    def do_POST(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            if path == "/upnp/control/contentdirectory" and self.srv.dlna:
                return self._upnp_soap("ContentDirectory")
            if path == "/upnp/control/connectionmanager" and self.srv.dlna:
                return self._upnp_soap("ConnectionManager")
            if path == "/api/auth/register":
                return self._api_register()
            if path == "/api/auth/login":
                return self._api_login()
            if path == "/api/auth/logout":
                return self._api_logout()
            if path == "/api/admin/login":
                return self._api_admin_login()
            if path == "/api/admin/logout":
                return self._api_admin_logout()
            if path == "/api/links":
                return self._api_create_link()
            if path == "/api/stream-keys":
                return self._api_create_stream_key()
            if path.startswith("/ingest/"):
                return self._ingest(path[len("/ingest/"):], parsed.query)
            if path.startswith("/api/admin/live/"):
                self._require_admin()
                parts = path[len("/api/admin/live/"):].split("/", 1)
                sid = urllib.parse.unquote(parts[0])
                action = parts[1] if len(parts) > 1 else None
                if action == "kick":
                    ok = self.srv.ingest.kick(sid)
                    return self._json({"id": sid, "kicked": ok})
                raise HttpError(404, "unknown action")
            if path.startswith("/api/admin/users/"):
                self._require_admin()
                parts = path[len("/api/admin/users/"):].split("/", 1)
                user = urllib.parse.unquote(parts[0])
                action = parts[1] if len(parts) > 1 else None
                if action == "approve":
                    return self._json({"user": self.srv.store.approve_user(user)})
                if action == "reject":
                    self.srv.store.delete_user(user)
                    return self._json({"user": user, "rejected": True})
                raise HttpError(404, "unknown action")
            if path.startswith("/api/streams/"):
                self._require_admin()
                mid = path[len("/api/streams/"):]
                full = resolve_source(self.srv.source_dir, mid)
                if not full:
                    raise HttpError(404, "unknown media")
                url, err = self.srv.hls.start(full, mid)
                if err:
                    raise HttpError(409, err)
                return self._json({"id": mid, "url": url}, 201)
            raise HttpError(404, "not found")
        except HttpError as e:
            self._err(e.code, e.message)
        except StoreError as e:
            self._err(400, str(e))
        except Exception:
            import traceback
            traceback.print_exc()
            self._err(500, "internal error")

    # ---------- auth / links API ----------
    def _api_register(self):
        body = self._read_json()
        user = self.srv.store.create_user(
            str(body.get("username", "")), str(body.get("password", "")))
        return self._json({"username": user["username"], "status": user["status"]}, 201)

    def _api_login(self):
        body = self._read_json()
        username = str(body.get("username", ""))
        u = self.srv.store.verify_user(username, str(body.get("password", "")))
        if not u:
            raise HttpError(401, "用户名或密码错误")
        if u["status"] == "pending":
            raise HttpError(403, "账号待管理员审批，请稍后再试")
        token = self.srv.store.create_session("client", username)
        self._json_cookie({"user": u, "status": "ok"}, 200, COOKIE_CLIENT, token, 24 * 3600)
        return

    def _api_logout(self):
        token = self._cookies().get(COOKIE_CLIENT)
        if token:
            self.srv.store.delete_session(token)
        self._json_cookie({"ok": True}, 200, COOKIE_CLIENT, "", 0)
        return

    def _api_admin_login(self):
        body = self._read_json()
        if not self.srv.store.verify_admin(
                str(body.get("username", "")), str(body.get("password", ""))):
            time.sleep(0.6)
            raise HttpError(401, "管理员用户名或密码错误")
        token = self.srv.store.create_session("admin", "admin")
        self._json_cookie({"status": "ok", "admin": True}, 200, COOKIE_ADMIN, token, 24 * 3600)
        return

    def _api_admin_logout(self):
        token = self._cookies().get(COOKIE_ADMIN)
        if token:
            self.srv.store.delete_session(token)
        self._json_cookie({"ok": True}, 200, COOKIE_ADMIN, "", 0)
        return

    def _api_create_link(self):
        if self._admin():
            username = "admin"
        else:
            username = self._require_client()["username"]
        body = self._read_json()
        mid = str(body.get("media_id", ""))
        live_id = str(body.get("live_id", ""))
        ttl = body.get("ttl") or 1800
        if mid:
            if not resolve_source(self.srv.source_dir, mid):
                raise HttpError(404, "媒体不存在")
            token, expires, link_id = self.srv.store.create_link(
                username, mid, ttl, kind="media")
            return self._json({
                "url": "/play/%s?token=%s" % (quote_relpath(mid), token),
                "token": token, "id": link_id, "media": mid,
                "kind": "media", "expires": expires,
            }, 201)
        if live_id:
            if not STREAM_ID_RE.match(live_id):
                raise HttpError(400, "流名称格式非法")
            if not self.srv.ingest.get(live_id):
                raise HttpError(404, "直播流不存在或已下线")
            token, expires, link_id = self.srv.store.create_link(
                username, live_id, ttl, kind="live")
            return self._json({
                "url": "/live/%s?token=%s" % (quote_relpath(live_id), token),
                "token": token, "id": link_id, "media": live_id,
                "kind": "live", "expires": expires,
            }, 201)
        raise HttpError(400, "缺少 media_id 或 live_id")

    def _api_create_stream_key(self):
        if self._admin():
            username = "admin"
        else:
            username = self._require_client()["username"]
        body = self._read_json()
        sid = str(body.get("stream_id", ""))
        ttl = body.get("ttl") or 3600
        if not STREAM_ID_RE.match(sid):
            raise HttpError(400, "流名称需 3-64 位，仅限字母/数字/_-")
        token, expires, key_id = self.srv.store.create_stream_key(username, sid, ttl)
        return self._json({
            "url": "/ingest/%s?key=%s" % (quote_relpath(sid), token),
            "key": token, "id": key_id, "stream_id": sid, "expires": expires,
        }, 201)

    # ---------- HTTP-TS ingest / live relay ----------
    def _ingest(self, stream_id, query):
        """MPEG-TS over HTTP POST. Body is a continuous 188-byte TS stream."""
        if not STREAM_ID_RE.match(stream_id):
            raise HttpError(400, "流名称格式非法")
        key = urllib.parse.parse_qs(query).get("key", [None])[0]
        owner = self.srv.store.check_stream_key(key, stream_id)
        if not owner:
            raise HttpError(401, "推流密钥无效或已过期")
        st, err = self.srv.ingest.acquire(stream_id, owner)
        if not st:
            raise HttpError(409, err)
        st.set_conn(self.connection)
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
        try:
            self.connection.settimeout(60.0)
            while not st.aborted:
                try:
                    chunk = next(self._body_chunks(chunked))
                except StopIteration:
                    break
                except (socket.timeout, TimeoutError, BrokenPipeError,
                        ConnectionResetError, OSError):
                    break
                if not chunk:
                    break
                st.push(chunk)
        finally:
            self.srv.ingest.release(stream_id)
        try:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except OSError:
            pass

    def _body_chunks(self, chunked):
        """Yield raw body chunks. Decodes chunked transfer-encoding when
        the publisher uses it (VLC/ffmpeg HTTP output often does)."""
        if chunked:
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                try:
                    size = int(line.strip().split(b";")[0], 16)
                except (ValueError, IndexError):
                    break
                if size == 0:
                    self.rfile.readline()  # consume trailing CRLF
                    break
                data = self.rfile.read(size)
                self.rfile.readline()  # consume trailing CRLF
                yield data
        else:
            while True:
                data = self.rfile.read1(65536)
                if not data:
                    break
                yield data

    def _live(self, stream_id, query):
        """Chunked MPEG-TS relay for viewers (VLC network stream)."""
        params = urllib.parse.parse_qs(query)
        token = params.get("token", [None])[0]
        if not self.srv.store.check_link(token, stream_id) and not self._admin():
            raise HttpError(401, "观看链接无效或已过期，请重新申请")
        st = self.srv.ingest.get(stream_id)
        if not st or not st.online:
            raise HttpError(404, "直播流不存在或已下线")
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        st.register_viewer()
        try:
            seq, first = st.snapshot()
            if first:
                self._chunk_write(first)
            while True:
                data, seq = st.wait_more(seq)
                if data is None:
                    self._chunk_write(b"")
                    break
                if data:
                    self._chunk_write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            st.unregister_viewer()
        try:
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass

    def _chunk_write(self, data):
        self.wfile.write(("%x\r\n" % len(data)).encode("ascii"))
        if data:
            self.wfile.write(data)
        self.wfile.write(b"\r\n")

    def _upnp_soap(self, service):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_048_576:
            raise HttpError(400, "bad body")
        body = self.rfile.read(length).decode("utf-8", "replace")
        soapaction = self.headers.get("SOAPACTION", "")
        code, _ct, xml = self.srv.dlna.handle_soap(service, soapaction, body)
        payload = xml.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_DELETE(self):
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            if path.startswith("/api/links/"):
                u = self._require_client()
                link_id = path[len("/api/links/"):]
                links = self.srv.store.list_links(None if self._admin() else u["username"])
                if not any(l["id"] == link_id for l in links):
                    raise HttpError(404, "链接不存在或无权操作")
                self.srv.store.revoke_link(link_id)
                return self._json({"id": link_id, "revoked": True})
            if path.startswith("/api/stream-keys/"):
                u = self._require_client()
                key_id = path[len("/api/stream-keys/"):]
                keys = self.srv.store.list_stream_keys(
                    None if self._admin() else u["username"])
                if not any(k["id"] == key_id for k in keys):
                    raise HttpError(404, "密钥不存在或无权操作")
                self.srv.store.revoke_stream_key(key_id)
                return self._json({"id": key_id, "revoked": True})
            if path.startswith("/api/admin/users/"):
                self._require_admin()
                user = urllib.parse.unquote(path[len("/api/admin/users/"):])
                ok = self.srv.store.delete_user(user)
                return self._json({"user": user, "removed": ok})
            if path.startswith("/api/admin/links/"):
                self._require_admin()
                link_id = path[len("/api/admin/links/"):]
                ok = self.srv.store.revoke_link(link_id)
                return self._json({"id": link_id, "revoked": ok})
            if path.startswith("/api/streams/"):
                self._require_admin()
                mid = path[len("/api/streams/"):]
                ok = self.srv.hls.stop(mid)
                return self._json({"id": mid, "stopped": ok})
            raise HttpError(404, "not found")
        except HttpError as e:
            self._err(e.code, e.message)
        except Exception:
            import traceback
            traceback.print_exc()
            self._err(500, "internal error")

    # ---------- handlers ----------
    def _static(self, path):
        rel = path[len("/static/"):]
        from .util import safe_join
        full = safe_join(self.srv.static_dir, rel)
        if not full or not os.path.isfile(full):
            raise HttpError(404, "not found")
        self._send_file(full, guess_mime(full))

    def _client_static(self, rel):
        from .util import safe_join
        rel = rel or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        full = safe_join(self.srv.client_dir, rel)
        if not full or not os.path.isfile(full):
            raise HttpError(404, "not found")
        self._send_file(full, guess_mime(full))

    def _play(self, media_id, query, require_auth):
        full = resolve_source(self.srv.source_dir, media_id)
        if not full:
            raise HttpError(404, "媒体不存在: %s" % media_id)
        if require_auth:
            params = urllib.parse.parse_qs(query)
            token = params.get("token", [None])[0]
            if not self.srv.store.check_link(token, media_id) and not self._admin():
                raise HttpError(401, "链接无效或已过期，请重新申请播放链接")
        attach = "download" in urllib.parse.parse_qs(query)
        self._send_file(full, guess_mime(full), self.headers.get("Range"), attach=attach)

    def _hls_file(self, rel):
        if not self.srv.hls.available:
            raise HttpError(404, "HLS disabled")
        from .util import safe_join
        full = safe_join(self.srv.hls_dir, rel)
        if not full or not os.path.isfile(full):
            raise HttpError(404, "playlist not ready")
        if rel.endswith(".m3u8"):
            self._send_file(full, "application/vnd.apple.mpegurl", None, attach=False)
        else:
            self._send_file(full, "video/mp2t", self.headers.get("Range"))

    # ---------- http ----------
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def version_string(self):
        return self.server_version
