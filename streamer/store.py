"""JSON-file backed store: admin, users (whitelist), sessions, time-limited links."""

import json
import os
import re
import threading
import time

from .auth import check_token, new_token, token_hash

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
PASSWORD_MIN = 8

SESSION_TTL = 24 * 3600
MAX_LINK_TTL = 6 * 3600
CLEANUP_INTERVAL = 300


class StoreError(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._last_cleanup = 0.0
        self._db = None
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.load()

    # ---------- persistence ----------
    def load(self):
        with self._lock:
            if os.path.isfile(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        self._db = json.load(f)
                except (OSError, ValueError):
                    self._db = self._empty()
            else:
                self._db = self._empty()
            for key in ("admin", "users", "sessions", "links", "stream_keys"):
                self._db.setdefault(key, {} if key != "admin" else None)

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._db, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def _maybe_cleanup(self):
        now = time.time()
        if now - self._last_cleanup < CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        expired = [k for k, v in self._db["sessions"].items() if v["expires"] < now]
        for k in expired:
            del self._db["sessions"][k]
        expired = [k for k, v in self._db["links"].items() if v["expires"] < now]
        for k in expired:
            del self._db["links"][k]
        if expired:
            self._save()

    @staticmethod
    def _empty():
        return {"admin": None, "users": {}, "sessions": {}, "links": {},
                "stream_keys": {}}

    # ---------- validation ----------
    @staticmethod
    def validate_credentials(username, password):
        if not username or not USERNAME_RE.match(username):
            raise StoreError("用户名需 3-32 位，仅限字母/数字/._-")
        if not password or len(password) < PASSWORD_MIN:
            raise StoreError("密码至少 %d 位" % PASSWORD_MIN)

    # ---------- admin ----------
    def ensure_admin(self, username, password):
        """Create the admin record on first run only. Password changes on
        later runs are ignored unless apply_admin_credentials is called."""
        with self._lock:
            if not self._db["admin"]:
                from .auth import hash_password
                self._db["admin"] = {"username": username, "pw_hash": hash_password(password)}
                self._save()
                return True
            return False

    def apply_admin_credentials(self, username, password):
        """Explicitly set the admin username/password (e.g. when the operator
        passes --admin-user/--admin-pass). Returns True if anything changed."""
        from .auth import hash_password, verify_password
        with self._lock:
            adm = self._db["admin"]
            if not adm:
                self._db["admin"] = {"username": username, "pw_hash": hash_password(password)}
                self._save()
                return True
            changed = False
            if adm["username"] != username:
                adm["username"] = username
                changed = True
            try:
                same = verify_password(adm["pw_hash"], password)
            except Exception:
                same = False
            if not same:
                adm["pw_hash"] = hash_password(password)
                changed = True
            if changed:
                self._save()
            return changed

    def get_admin_username(self):
        with self._lock:
            return (self._db["admin"] or {}).get("username")

    def verify_admin(self, username, password):
        with self._lock:
            adm = self._db["admin"]
        if not adm or adm["username"] != username:
            from .auth import hash_password, verify_password
            verify_password(hash_password("dummy"), "dummy")  # timing equalization
            return False
        from .auth import verify_password
        return verify_password(adm["pw_hash"], password)

    # ---------- users ----------
    def create_user(self, username, password):
        self.validate_credentials(username, password)
        from .auth import hash_password
        with self._lock:
            if username in self._db["users"] or (self._db["admin"] and self._db["admin"]["username"] == username):
                raise StoreError("用户名已存在")
            self._db["users"][username] = {
                "username": username,
                "pw_hash": hash_password(password),
                "status": "pending",
                "created": time.time(),
                "approved": None,
            }
            self._save()
        return self._db["users"][username]

    def verify_user(self, username, password):
        with self._lock:
            u = self._db["users"].get(username)
        if not u:
            from .auth import verify_password
            verify_password("$argon2id$v=19$m=32768,t=2,p=2$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "x")
            return None
        from .auth import verify_password
        if not verify_password(u["pw_hash"], password):
            return None
        return u

    def get_user(self, username):
        with self._lock:
            return dict(self._db["users"].get(username, {}) or {})

    def list_users(self):
        with self._lock:
            return [dict(v) for v in self._db["users"].values()]

    def approve_user(self, username):
        with self._lock:
            u = self._db["users"].get(username)
            if not u:
                raise StoreError("用户不存在")
            u["status"] = "active"
            u["approved"] = time.time()
            self._save()
        return u

    def delete_user(self, username):
        with self._lock:
            if username not in self._db["users"]:
                return False
            del self._db["users"][username]
            for k in [k for k, v in self._db["sessions"].items() if v["user"] == username]:
                del self._db["sessions"][k]
            for k in [k for k, v in self._db["links"].items() if v["user"] == username]:
                del self._db["links"][k]
            for k in [k for k, v in self._db["stream_keys"].items() if v["user"] == username]:
                del self._db["stream_keys"][k]
            self._save()
        return True

    # ---------- sessions ----------
    def create_session(self, kind, user):
        token = new_token()
        with self._lock:
            self._db["sessions"][token] = {
                "kind": kind, "user": user, "expires": time.time() + SESSION_TTL,
            }
            self._save()
        return token

    def get_session(self, token):
        if not token:
            return None
        with self._lock:
            s = self._db["sessions"].get(token)
            if not s:
                return None
            if s["expires"] < time.time():
                del self._db["sessions"][token]
                self._save()
                return None
            return dict(s)

    def delete_session(self, token):
        with self._lock:
            if token in self._db["sessions"]:
                del self._db["sessions"][token]
                self._save()

    # ---------- time-limited media links ----------
    def create_link(self, user, target, ttl, kind="media"):
        ttl = max(60, min(int(ttl or 1800), MAX_LINK_TTL))
        token = new_token()
        with self._lock:
            self._maybe_cleanup()
            h = token_hash(token)
            self._db["links"][h] = {
                "user": user, "target": target, "kind": kind,
                "created": time.time(), "expires": time.time() + ttl,
            }
            self._save()
        return token, self._db["links"][h]["expires"], h[:16]

    def check_link(self, token, target):
        if not token:
            return False
        with self._lock:
            h = token_hash(token)
            l = self._db["links"].get(h)
            if not l:
                return False
            if l["expires"] < time.time():
                del self._db["links"][h]
                self._save()
                return False
            return l.get("target", l.get("media", "")) == target

    def list_links(self, user=None):
        now = time.time()
        with self._lock:
            out = []
            for h, l in self._db["links"].items():
                if l["expires"] < now:
                    continue
                if user and l["user"] != user:
                    continue
                out.append({"id": h[:16], "user": l["user"],
                            "target": l.get("target", l.get("media", "")),
                            "kind": l.get("kind", "media"),
                            "created": l["created"], "expires": l["expires"]})
        out.sort(key=lambda x: x["expires"])
        return out

    def revoke_link(self, link_id):
        with self._lock:
            for h in list(self._db["links"].keys()):
                if h.startswith(link_id):
                    del self._db["links"][h]
                    self._save()
                    return True
        return False

    # ---------- publish stream keys (HTTP-TS ingest) ----------
    def create_stream_key(self, user, stream_id, ttl):
        ttl = max(60, min(int(ttl or 3600), MAX_LINK_TTL))
        token = new_token()
        with self._lock:
            self._maybe_cleanup()
            h = token_hash(token)
            self._db["stream_keys"][h] = {
                "user": user, "stream_id": stream_id,
                "created": time.time(), "expires": time.time() + ttl,
            }
            self._save()
        return token, self._db["stream_keys"][h]["expires"], h[:16]

    def check_stream_key(self, token, stream_id):
        """Validate an ingest key; returns owner username or None."""
        if not token:
            return None
        with self._lock:
            h = token_hash(token)
            k = self._db["stream_keys"].get(h)
            if not k:
                return None
            if k["expires"] < time.time():
                del self._db["stream_keys"][h]
                self._save()
                return None
            if k["stream_id"] != stream_id:
                return None
            return k["user"]

    def list_stream_keys(self, user=None):
        now = time.time()
        with self._lock:
            out = []
            for h, k in self._db["stream_keys"].items():
                if k["expires"] < now:
                    continue
                if user and k["user"] != user:
                    continue
                out.append({"id": h[:16], "user": k["user"],
                            "stream_id": k["stream_id"],
                            "created": k["created"], "expires": k["expires"]})
        out.sort(key=lambda x: x["expires"])
        return out

    def revoke_stream_key(self, key_id):
        with self._lock:
            for h in list(self._db["stream_keys"].keys()):
                if h.startswith(key_id):
                    del self._db["stream_keys"][h]
                    self._save()
                    return True
        return False
