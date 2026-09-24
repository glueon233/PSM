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
from streamer.util import ensure_firewall_rules  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Private streaming media server")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="listen port (default 8000)")
    parser.add_argument("--source", default="source", help="media source directory")
    parser.add_argument("--hls-dir", default="hls", help="HLS output directory")
    parser.add_argument("--data-dir", default="data", help="user/link database directory")
    parser.add_argument("--device-name", default="StreamMedia", help="DLNA device friendly name")
    parser.add_argument("--no-dlna", action="store_true", help="disable DLNA/UPnP")
    parser.add_argument("--dlna-keep-ssdpsrv", action="store_true",
                        help="keep the Windows SSDP service running (for AIMP/WMP), "
                             "accepting less reliable LAN discovery. Default: stop it "
                             "for reliable DLNA discovery on TVs/phones/VLC.")
    parser.add_argument("--admin-user", default="admin", help="admin username")
    parser.add_argument("--admin-pass", default=None,
                        help="admin password. Sets the password on first run; "
                             "on later runs, updates the stored password. "
                             "Default when omitted: admin123 (first run only)")
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
    if args.admin_pass is not None:
        changed = httpd.store.apply_admin_credentials(args.admin_user, args.admin_pass)
        if changed:
            print("  [admin] credentials set for account '%s'" % args.admin_user)
    else:
        created = httpd.store.ensure_admin(args.admin_user, "admin123")
        if created:
            print("  [admin] account '%s' created with DEFAULT password admin123" % args.admin_user)

    host = args.host if args.host not in ("0.0.0.0", "::") else "localhost"
    print("=" * 62)
    print("  StreamMedia server running")
    print("  Client portal   : http://%s:%d/client/" % (host, httpd.server_port))
    print("  Admin console   : http://%s:%d/admin" % (host, httpd.server_port))
    print("  Media streaming : http://%s:%d/play/<id>?token=<限时链接>" % (host, httpd.server_port))
    print("  Source dir      : %s" % source_dir)
    print("  Admin account   : %s" % (httpd.store.get_admin_username() or args.admin_user))
    print("  HLS live mode   : %s" % ("enabled (ffmpeg)" if httpd.hls.ffmpeg else "disabled (ffmpeg not found)"))
    print("  VLC: 媒体 -> 打开网络串流 -> 粘贴限时链接")
    print("=" * 62)

    fw = ensure_firewall_rules(httpd.server_port)
    if fw:
        print("  Firewall: %s" % fw)
        print("  (needed for LAN devices to reach the server; runs as admin)")

    if not args.no_dlna:
        uuid_path = os.path.join(base, ".dlna_uuid")
        dlna = DlnaServer(args.host, httpd.server_port, source_dir,
                          friendly_name=args.device_name, uuid_path=uuid_path,
                          auto_fix_ssdp=not args.dlna_keep_ssdpsrv)
        httpd.dlna = dlna
        dlna.start()
        print("  DLNA: device '%s' (AIMP: 音乐库 -> DLNA; VLC: 本地网络)" % args.device_name)
        print("=" * 62)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down ...")
    finally:
        if httpd.dlna:
            httpd.dlna.stop()
        httpd.hls.stop_all()
        httpd.ingest.stop_all()
        httpd.server_close()


if __name__ == "__main__":
    main()
