#!/usr/bin/env python3
"""Streaming media server entry point.

Usage:
    python server.py [--host 0.0.0.0] [--port 8000] [--source source]

Playback in VLC:
    媒体 -> 打开网络串流 -> http://<host>:<port>/play/<文件名>
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamer.app import StreamingServer, Handler  # noqa: E402
from streamer.dlna import DlnaServer  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Private streaming media server")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="listen port (default 8000)")
    parser.add_argument("--source", default="source", help="media source directory")
    parser.add_argument("--hls-dir", default="hls", help="HLS output directory")
    parser.add_argument("--data-dir", default="data", help="user/link database directory")
    parser.add_argument("--device-name", default="StreamMedia", help="DLNA device friendly name")
    parser.add_argument("--no-dlna", action="store_true", help="disable DLNA/UPnP")
    parser.add_argument("--dlna-no-auto-fix", action="store_true",
                        help="do not try to stop Windows SSDPSRV service (DLNA discovery may fail)")
    parser.add_argument("--admin-user", default="admin", help="admin username (first run only)")
    parser.add_argument("--admin-pass", default="admin123", help="admin password (first run only)")
    args = parser.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))
    source_dir = os.path.abspath(args.source)
    hls_dir = os.path.abspath(args.hls_dir)
    data_dir = os.path.abspath(args.data_dir)
    static_dir = os.path.join(base, "static")
    client_dir = os.path.join(base, "client")

    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(hls_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)

    httpd = StreamingServer((args.host, args.port), Handler, source_dir, hls_dir,
                            static_dir, client_dir, data_dir)
    httpd.store.ensure_admin(args.admin_user, args.admin_pass)

    host = args.host if args.host not in ("0.0.0.0", "::") else "localhost"
    print("=" * 62)
    print("  StreamMedia server running")
    print("  Client portal   : http://%s:%d/client/" % (host, httpd.server_port))
    print("  Admin console   : http://%s:%d/admin" % (host, httpd.server_port))
    print("  Media streaming : http://%s:%d/play/<id>?token=<限时链接>" % (host, httpd.server_port))
    print("  Source dir      : %s" % source_dir)
    print("  Admin account   : %s (created on first run)" % args.admin_user)
    if args.admin_pass == "admin123":
        print("  WARNING: default admin password in use, change it via --admin-pass on first run")
    print("  HLS live mode   : %s" % ("enabled (ffmpeg)" if httpd.hls.ffmpeg else "disabled (ffmpeg not found)"))
    print("  VLC: 媒体 -> 打开网络串流 -> 粘贴限时链接")
    print("=" * 62)

    if not args.no_dlna:
        uuid_path = os.path.join(base, ".dlna_uuid")
        dlna = DlnaServer(args.host, httpd.server_port, source_dir,
                          friendly_name=args.device_name, uuid_path=uuid_path,
                          auto_fix_ssdp=not args.dlna_no_auto_fix)
        httpd.dlna = dlna
        dlna.start()
        print("  DLNA: device '%s' (VLC: 本地网络 -> Universal Plug'n'Play)" % args.device_name)
        print("=" * 62)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down ...")
    finally:
        if httpd.dlna:
            httpd.dlna.stop()
        httpd.hls.stop_all()
        httpd.server_close()


if __name__ == "__main__":
    main()
