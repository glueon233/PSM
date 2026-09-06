"""DLNA/UPnP MediaServer: SSDP discovery + ContentDirectory SOAP + ConnectionManager.

Pure stdlib. Exposes the source library to any DLNA control point
(VLC, TVs, phones). Media is served over plain HTTP with Range support.
"""

import html
import os
import re
import select
import socket
import struct
import subprocess
import threading
import time
import uuid

from .library import scan_library
from .util import MEDIA_EXTS, guess_mime, quote_relpath, safe_join

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
CACHE_MAX_AGE = 1800
SERVER_HEADER = "Windows/10 UPnP/1.0 StreamMedia/1.0"

DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
SERVICE_CDS = "urn:schemas-upnp-org:service:ContentDirectory:1"
SERVICE_CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"

DLNA_FLAGS = "01700000000000000000000000000000"

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a",
              ".dsf", ".dff", ".dsd", ".wv", ".ape", ".opus"}
PN_MAP = {
    ".mp4": "MP4", ".m4v": "MP4", ".avi": "AVI",
    ".mkv": "MATROSKA", ".mp3": "MP3",
}


def xml_escape(s):
    return html.escape(str(s), quote=False)


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class DlnaServer:
    def __init__(self, host, port, source_dir, friendly_name="StreamMedia",
                 uuid_path=None, auto_fix_ssdp=True):
        self.host = host
        self.port = port
        self.source_dir = source_dir
        self.friendly_name = friendly_name
        self.auto_fix_ssdp = auto_fix_ssdp
        self.enabled = False
        self.msearch_count = 0
        self._uuid = self._load_uuid(uuid_path)
        self.udn = "uuid:" + self._uuid
        self._sock = None
        self._tx = None
        self._joined = set()
        self._thread = None
        self._stop_evt = threading.Event()

    @property
    def advertise_ip(self):
        """Current LAN IP (dynamic: survives network/Wi-Fi changes)."""
        if self.host in ("0.0.0.0", "::", ""):
            return _lan_ip()
        return self.host

    # ---------- windows SSDP service handling ----------
    @staticmethod
    def _ssdpsrv_running():
        if os.name != "nt":
            return False
        try:
            out = subprocess.check_output(["sc", "query", "SSDPSRV"],
                                          stderr=subprocess.STDOUT)
            return b"RUNNING" in out
        except Exception:
            return False

    def _prepare_windows_ssdp(self):
        """On Windows, the built-in SSDP service (SSDPSRV) owns multicast
        delivery on port 1900. Two coexistence strategies exist:

        * SSDPSRV running: it caches our NOTIFY announcements and answers
          M-SEARCH on our behalf. Windows UPnP clients (AIMP plugin, WMP)
          can discover us; clients using their own SSDP stack (VLC) may not.
        * SSDPSRV stopped: direct SSDP clients (VLC, TVs, phones) work,
          but Windows UPnP based clients (AIMP plugin) lose discovery.

        Default: leave SSDPSRV alone. Only stop it when explicitly enabled
        with auto_fix_ssdp (requires admin).
        """
        if os.name != "nt" or not self._ssdpsrv_running():
            return
        if not self.auto_fix_ssdp:
            print("[dlna] Windows SSDP service (SSDPSRV) is running: our NOTIFY announcements")
            print("[dlna] will be cached by Windows. AIMP/WMP can discover the server.")
            print("[dlna] Same-host VLC discovery may not work in this mode (platform limitation).")
            return
        print("[dlna] trying to stop SSDPSRV for direct SSDP clients (VLC) ...")
        rc = subprocess.call(["net", "stop", "SSDPSRV"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if rc == 0:
            print("[dlna] stopped SSDPSRV (restore with: net start SSDPSRV)")
        else:
            print("[dlna] WARNING: cannot stop SSDPSRV (administrator required)")
            print("[dlna] WARNING: same-host VLC discovery may not work")
            print("[dlna] Fix: run this program as administrator, or execute: net stop SSDPSRV")

    # ---------- identity ----------
    @staticmethod
    def _load_uuid(uuid_path):
        try:
            with open(uuid_path, "r") as f:
                val = f.read().strip()
            if re.fullmatch(r"[0-9a-fA-F-]{36}", val):
                return val
        except Exception:
            pass
        val = str(uuid.uuid4())
        if uuid_path:
            try:
                with open(uuid_path, "w") as f:
                    f.write(val)
            except Exception:
                pass
        return val

    def status(self):
        return {
            "enabled": self.enabled,
            "friendly_name": self.friendly_name,
            "udn": self.udn,
            "ip": self.advertise_ip,
            "port": self.port,
            "msearch": self.msearch_count,
        }

    # ---------- lifecycle ----------
    def start(self):
        if self.enabled:
            return
        self._thread = threading.Thread(target=self._ssdp_loop, daemon=True,
                                        name="dlna-ssdp")
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        if self._sock:
            self._send_byebye()
            for s in (self._sock, getattr(self, "_tx", None)):
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
            self._sock = None
            self._tx = None
        self.enabled = False

    # ---------- SSDP ----------
    def _make_sock(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.bind(("", SSDP_PORT))
        self._joined = self._join_interfaces(sock)
        sock.settimeout(0.5)
        return sock

    def _join_interfaces(self, sock):
        joined = set()
        for iface in self._ipv4_interfaces():
            mreq = struct.pack("4s4s",
                               socket.inet_aton(SSDP_ADDR),
                               socket.inet_aton(iface))
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined.add(iface)
            except OSError:
                pass
        if not joined:  # fallback: default interface
            mreq = struct.pack("4s4s",
                               socket.inet_aton(SSDP_ADDR),
                               socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            joined.add("0.0.0.0")
        print("[dlna] SSDP multicast joined on: %s" % ", ".join(sorted(joined)))
        return joined

    def _rejoin_interfaces(self, sock):
        """Windows delivers multicast on a shared port only to the most
        recently joined socket; after another UPnP process (e.g. VLC) joins
        and exits, delivery can be left dangling. Drop and re-add membership
        to reclaim reception."""
        for iface in self._joined:
            try:
                mreq = struct.pack("4s4s",
                                   socket.inet_aton(SSDP_ADDR),
                                   socket.inet_aton(iface))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
            except OSError:
                pass
        self._joined = self._join_interfaces(sock)

    def _make_tx_sock(self):
        """Separate send-only socket. On Windows, sending can trigger ICMP
        errors which surface as ConnectionResetError on the *sending* socket's
        next recvfrom(); keeping TX apart from the RX socket avoids killing
        the receive loop."""
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        tx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            tx.bind(("", SSDP_PORT))
        except OSError:
            tx.bind(("", 0))
        return tx

    @staticmethod
    def _ipv4_interfaces():
        ips = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip.startswith(("127.", "169.254.")):
                    continue
                ips.add(ip)
        except OSError:
            pass
        return ips

    def _ssdp_loop(self):
        self._prepare_windows_ssdp()
        try:
            self._sock = self._make_sock()
            self._tx = self._make_tx_sock()
        except OSError as e:
            print("[dlna] SSDP disabled: cannot bind %s:%d (%s)"
                  % (SSDP_ADDR, SSDP_PORT, e))
            return
        self.enabled = True
        print("[dlna] enabled: device=%s udn=%s advertise=http://%s:%d"
              % (self.friendly_name, self.udn, self.advertise_ip, self.port))
        self._send_alive_all()
        last_alive = time.time()
        last_rejoin = time.time()
        while not self._stop_evt.is_set():
            now = time.time()
            if now - last_alive >= 60:
                self._send_alive_all()
                last_alive = now
            if now - last_rejoin >= 30:
                self._rejoin_interfaces(self._sock)
                last_rejoin = now
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except (ConnectionResetError, OSError):
                # Windows delivers ICMP errors via recvfrom; keep receiving.
                continue
            if data.startswith(b"M-SEARCH"):
                self._handle_msearch(data, addr)

    def _location(self):
        return "http://%s:%d/rootDesc.xml" % (self.advertise_ip, self.port)

    def _send_ssdp(self, payload):
        """Send a datagram to the SSDP multicast group from every interface."""
        tx = getattr(self, "_tx", None) or self._sock
        if not tx:
            return
        for iface in self._ipv4_interfaces():
            try:
                tx.setsockopt(
                    socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(iface))
                tx.sendto(payload, (SSDP_ADDR, SSDP_PORT))
            except OSError:
                pass

    def _notify(self, nt, usn, nts):
        msg = "\r\n".join([
            "NOTIFY * HTTP/1.1",
            "HOST: %s:%d" % (SSDP_ADDR, SSDP_PORT),
            "CACHE-CONTROL: max-age=%d" % CACHE_MAX_AGE,
            "LOCATION: %s" % self._location(),
            "NT: %s" % nt,
            "NTS: %s" % nts,
            "SERVER: %s" % SERVER_HEADER,
            "USN: %s" % usn,
            "", "",
        ]).encode("utf-8")
        self._send_ssdp(msg)

    def _send_alive_all(self):
        entries = [
            ("upnp:rootdevice", self.udn + "::upnp:rootdevice"),
            (self.udn, self.udn),
            (DEVICE_TYPE, self.udn + "::" + DEVICE_TYPE),
            (SERVICE_CDS, self.udn + "::" + SERVICE_CDS),
            (SERVICE_CMS, self.udn + "::" + SERVICE_CMS),
        ]
        for nt, usn in entries:
            self._notify(nt, usn, "ssdp:alive")

    def _send_byebye(self):
        entries = [
            ("upnp:rootdevice", self.udn + "::upnp:rootdevice"),
            (self.udn, self.udn),
            (DEVICE_TYPE, self.udn + "::" + DEVICE_TYPE),
            (SERVICE_CDS, self.udn + "::" + SERVICE_CDS),
            (SERVICE_CMS, self.udn + "::" + SERVICE_CMS),
        ]
        for nt, usn in entries:
            self._notify(nt, usn, "ssdp:byebye")

    def _handle_msearch(self, data, addr):
        text = data.decode("utf-8", "replace")
        if "ssdp:discover" not in text.lower():
            return
        st = re.search(r"(?im)^ST:\s*(.+?)\s*$", text)
        if not st:
            return
        st = st.group(1).strip()
        if st == "ssdp:all":
            st = "upnp:rootdevice"
        table = {
            "upnp:rootdevice": self.udn + "::upnp:rootdevice",
            self.udn: self.udn,
            DEVICE_TYPE: self.udn + "::" + DEVICE_TYPE,
            SERVICE_CDS: self.udn + "::" + SERVICE_CDS,
            SERVICE_CMS: self.udn + "::" + SERVICE_CMS,
        }
        if st not in table:
            return
        self.msearch_count += 1
        msg = "\r\n".join([
            "HTTP/1.1 200 OK",
            "CACHE-CONTROL: max-age=%d" % CACHE_MAX_AGE,
            "EXT:",
            "LOCATION: %s" % self._location(),
            "SERVER: %s" % SERVER_HEADER,
            "ST: %s" % st,
            "USN: %s" % table[st],
            "", "",
        ]).encode("utf-8")
        tx = getattr(self, "_tx", None) or self._sock
        try:
            tx.sendto(msg, addr)
        except OSError:
            pass

    # ---------- UPnP description documents ----------
    def device_xml(self, host_header=None):
        base = "http://%s/" % (host_header.split(",")[0].strip() if host_header else self._location().rsplit("/", 1)[0])
        return """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<URLBase>{base}</URLBase>
<device>
<deviceType>{devtype}</deviceType>
<friendlyName>{name}</friendlyName>
<manufacturer>StreamMedia</manufacturer>
<manufacturerURL>{base}</manufacturerURL>
<modelDescription>Private streaming media server</modelDescription>
<modelName>StreamMedia Server</modelName>
<modelNumber>1.0</modelNumber>
<modelURL>{base}</modelURL>
<serialNumber>{serial}</serialNumber>
<UDN>{udn}</UDN>
<dlna:X_DLNADOC xmlns:dlna="urn:schemas-dlna-org:device-1-0">DMS-1.50</dlna:X_DLNADOC>
<presentationURL>/</presentationURL>
<serviceList>
<service>
<serviceType>{cds}</serviceType>
<serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
<SCPDURL>/upnp/scpd/ContentDirectory.xml</SCPDURL>
<controlURL>/upnp/control/contentdirectory</controlURL>
<eventSubURL>/upnp/event/contentdirectory</eventSubURL>
</service>
<service>
<serviceType>{cms}</serviceType>
<serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
<SCPDURL>/upnp/scpd/ConnectionManager.xml</SCPDURL>
<controlURL>/upnp/control/connectionmanager</controlURL>
<eventSubURL>/upnp/event/connectionmanager</eventSubURL>
</service>
</serviceList>
</device>
</root>""".format(
            base=base,
            name=xml_escape(self.friendly_name),
            serial=xml_escape(self._uuid),
            udn=self.udn,
            devtype=DEVICE_TYPE,
            cds=SERVICE_CDS,
            cms=SERVICE_CMS,
        )

    def scpd(self, service):
        if service == "ContentDirectory":
            return """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<actionList>
<action><name>GetSearchCapabilities</name>
<argumentList><argument><name>SearchCaps</name><direction>out</direction><relatedStateVariable>SearchCapabilities</relatedStateVariable></argument></argumentList></action>
<action><name>GetSortCapabilities</name>
<argumentList><argument><name>SortCaps</name><direction>out</direction><relatedStateVariable>SortCapabilities</relatedStateVariable></argument></argumentList></action>
<action><name>GetSystemUpdateID</name>
<argumentList><argument><name>Id</name><direction>out</direction><relatedStateVariable>SystemUpdateID</relatedStateVariable></argument></argumentList></action>
<action><name>Browse</name>
<argumentList>
<argument><name>ObjectID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>
<argument><name>BrowseFlag</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>
<argument><name>Filter</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>
<argument><name>StartingIndex</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>
<argument><name>RequestedCount</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
<argument><name>SortCriteria</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>
<argument><name>Result</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>
<argument><name>NumberReturned</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
<argument><name>TotalMatches</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>
<argument><name>UpdateID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_UpdateID</relatedStateVariable></argument>
</argumentList></action>
</actionList>
<serviceStateTable>
<stateVariable sendEvents="no"><name>SearchCapabilities</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="no"><name>SortCapabilities</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>SystemUpdateID</name><dataType>ui4</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name><dataType>ui4</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name><dataType>ui4</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>A_ARG_TYPE_Result</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_UpdateID</name><dataType>ui4</dataType></stateVariable>
</serviceStateTable>
</scpd>"""
        if service == "ConnectionManager":
            return """<?xml version="1.0"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>
<actionList>
<action><name>GetProtocolInfo</name>
<argumentList>
<argument><name>Source</name><direction>out</direction><relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>
<argument><name>Sink</name><direction>out</direction><relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>
</argumentList></action>
<action><name>GetCurrentConnectionIDs</name>
<argumentList><argument><name>ConnectionIDs</name><direction>out</direction><relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument></argumentList></action>
<action><name>GetCurrentConnectionInfo</name>
<argumentList>
<argument><name>ConnectionID</name><direction>in</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
<argument><name>RcsID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_RcsID</relatedStateVariable></argument>
<argument><name>AVTransportID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_AVTransportID</relatedStateVariable></argument>
<argument><name>ProtocolInfo</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ProtocolInfo</relatedStateVariable></argument>
<argument><name>PeerConnectionManager</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionManager</relatedStateVariable></argument>
<argument><name>PeerConnectionID</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>
<argument><name>Direction</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_Direction</relatedStateVariable></argument>
<argument><name>Status</name><direction>out</direction><relatedStateVariable>A_ARG_TYPE_ConnectionStatus</relatedStateVariable></argument>
</argumentList></action>
</actionList>
<serviceStateTable>
<stateVariable sendEvents="yes"><name>SourceProtocolInfo</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>SinkProtocolInfo</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="yes"><name>CurrentConnectionIDs</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name><dataType>i4</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name><dataType>i4</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name><dataType>i4</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name><dataType>string</dataType></stateVariable>
<stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name><dataType>string</dataType></stateVariable>
</serviceStateTable>
</scpd>"""
        return None

    # ---------- SOAP control ----------
    @staticmethod
    def _strip_ns(body):
        body = re.sub(r"<(\w+):(\w+)([ >])", r"<\2\3", body)
        body = re.sub(r"</(\w+):(\w+)>", r"</\2>", body)
        return body

    @staticmethod
    def _tag(body, name):
        m = re.search(r"<%s(?: [^>]*)?>(.*?)</%s>" % (name, name), body, re.S)
        return html.unescape(m.group(1).strip()) if m else None

    @staticmethod
    def _soap_response(service, action, fields):
        inner = "".join("<%s>%s</%s>" % (k, v, k) for k, v in fields.items())
        return (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body>"
            '<u:%sResponse xmlns:u="%s">%s</u:%sResponse>'
            "</s:Body></s:Envelope>"
        ) % (action, service, inner, action)

    @staticmethod
    def _soap_fault(code, desc):
        return (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            "<s:Body><s:Fault><faultcode>s:Client</faultcode>"
            "<faultstring>UPnPError</faultstring><detail>"
            '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
            "<errorCode>%s</errorCode><errorDescription>%s</errorDescription>"
            "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
        ) % (code, xml_escape(desc))

    def handle_soap(self, service, soapaction, body):
        action = soapaction.split("#")[-1].strip('"').strip()
        if service == "ContentDirectory":
            return self._soap_cds(action, body)
        if service == "ConnectionManager":
            return self._soap_cms(action, body)
        return 404, "text/xml", "unknown service"

    def _soap_cds(self, action, body):
        body = self._strip_ns(body or "")
        if action == "GetSearchCapabilities":
            return 200, "text/xml", self._soap_response(SERVICE_CDS, action, {"SearchCaps": "dc:title"})
        if action == "GetSortCapabilities":
            return 200, "text/xml", self._soap_response(SERVICE_CDS, action, {"SortCaps": "dc:title"})
        if action == "GetSystemUpdateID":
            return 200, "text/xml", self._soap_response(SERVICE_CDS, action, {"Id": "1"})
        if action == "Browse":
            oid = self._tag(body, "ObjectID")
            flag = self._tag(body, "BrowseFlag")
            try:
                start = int(self._tag(body, "StartingIndex") or 0)
                count = int(self._tag(body, "RequestedCount") or 0)
            except ValueError:
                return 500, "text/xml", self._soap_fault(402, "Invalid Args")
            result, n, total = self._browse(oid, flag, start, count)
            if result is None:
                return 500, "text/xml", self._soap_fault(701, "No such object")
            return 200, "text/xml", self._soap_response(SERVICE_CDS, action, {
                "Result": html.escape(result),
                "NumberReturned": str(n),
                "TotalMatches": str(total),
                "UpdateID": "1",
            })
        return 500, "text/xml", self._soap_fault(401, "Invalid Action")

    def _soap_cms(self, action, body):
        if action == "GetProtocolInfo":
            protos = [
                "http-get:*:video/mp4:*",
                "http-get:*:video/x-msvideo:*",
                "http-get:*:video/x-matroska:*",
                "http-get:*:video/quicktime:*",
                "http-get:*:video/x-flv:*",
                "http-get:*:video/x-ms-wmv:*",
                "http-get:*:video/mp2t:*",
                "http-get:*:video/webm:*",
                "http-get:*:audio/mpeg:*",
                "http-get:*:audio/wav:*",
                "http-get:*:audio/flac:*",
                "http-get:*:audio/aac:*",
                "http-get:*:audio/ogg:*",
                "http-get:*:audio/x-dsf:*",
                "http-get:*:audio/x-dff:*",
                "http-get:*:audio/dsd:*",
                "http-get:*:audio/x-wavpack:*",
                "http-get:*:audio/x-ape:*",
                "http-get:*:audio/opus:*",
            ]
            return 200, "text/xml", self._soap_response(SERVICE_CMS, action, {
                "Source": ",".join(protos), "Sink": ""})
        if action == "GetCurrentConnectionIDs":
            return 200, "text/xml", self._soap_response(SERVICE_CMS, action, {"ConnectionIDs": ""})
        if action == "GetCurrentConnectionInfo":
            return 500, "text/xml", self._soap_fault(706, "No Such Connection")
        return 500, "text/xml", self._soap_fault(401, "Invalid Action")

    # ---------- ContentDirectory browse ----------
    def _children(self, oid):
        """Return list of DIDL entries for a container id."""
        if oid in (None, "", "0"):
            base = self.source_dir
        elif oid.startswith("C:"):
            rel = oid[2:].replace("/", os.sep)
            full = safe_join(self.source_dir, rel)
            if not full or not os.path.isdir(full):
                return None
            base = full
        else:
            return None
        entries = []
        try:
            names = sorted(os.listdir(base), key=str.lower)
        except OSError:
            return None
        for name in names:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, self.source_dir)
            if os.path.isdir(full):
                entries.append(self._container_entry(rel, name))
            elif name.lower().endswith(MEDIA_EXTS):
                entries.append(self._item_entry(rel, name))
        return entries

    def _container_entry(self, rel, name):
        full = os.path.join(self.source_dir, rel)
        try:
            child_count = len(os.listdir(full))
        except OSError:
            child_count = 0
        return ('<container id="C:%s" parentID="%s" restricted="1" childCount="%d">'
                "<dc:title>%s</dc:title>"
                "<upnp:class>object.container.storageFolder</upnp:class>"
                "</container>") % (
            xml_escape(rel.replace(os.sep, "/")),
            xml_escape("0" if os.sep not in rel else "C:" + os.path.dirname(rel).replace(os.sep, "/")),
            child_count, xml_escape(name))

    def _item_entry(self, rel, name):
        full = os.path.join(self.source_dir, rel)
        ext = os.path.splitext(name)[1].lower()
        is_audio = ext in AUDIO_EXTS
        cls = "object.item.audioItem.musicTrack" if is_audio else "object.item.videoItem"
        from .library import probe
        from .tags import read_tags_for
        info = probe(full)
        if info.get("size") is None:
            try:
                info["size"] = os.path.getsize(full)
            except OSError:
                pass
        res_attrs = []
        if info.get("size"):
            res_attrs.append('size="%d"' % info["size"])
        if info.get("duration"):
            res_attrs.append('duration="%s"' % self._fmt_duration(info["duration"]))
        if info.get("bitrate"):
            res_attrs.append('bitrate="%d"' % info["bitrate"])
        if info.get("sample_rate"):
            res_attrs.append('sampleFrequency="%d"' % info["sample_rate"])
        if info.get("channels"):
            res_attrs.append('nrAudioChannels="%d"' % info["channels"])
        pn = PN_MAP.get(ext)
        if pn:
            pi = "http-get:*:%s:DLNA.ORG_PN=%s;DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=%s" % (
                guess_mime(full), pn, DLNA_FLAGS)
        else:
            pi = "http-get:*:%s:*" % guess_mime(full)
        url = "http://%s:%d/MediaItems/%s" % (self.advertise_ip, self.port, quote_relpath(rel))
        parent = "0" if os.sep not in rel else "C:" + os.path.dirname(rel).replace(os.sep, "/")
        meta = ""
        title = name
        if is_audio:
            tags = read_tags_for(full)
            if tags.get("title"):
                title = tags["title"]
            for key, tag in (("artist", "upnp:artist"), ("album", "upnp:album"),
                             ("genre", "upnp:genre"), ("year", "upnp:year")):
                if tags.get(key):
                    meta += "<%s>%s</%s>" % (tag, xml_escape(tags[key]), tag)
        return ('<item id="I:%s" parentID="%s" restricted="1">'
                "<dc:title>%s</dc:title>"
                '<upnp:class>%s</upnp:class>%s'
                '<res protocolInfo="%s" %s>%s</res>'
                "</item>") % (
            xml_escape(rel.replace(os.sep, "/")),
            xml_escape(parent),
            xml_escape(title),
            cls,
            meta,
            xml_escape(pi),
            " ".join(res_attrs),
            xml_escape(url))

    @staticmethod
    def _fmt_duration(sec):
        ms = int(round(sec * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return "%d:%02d:%02d.%03d" % (h, m, s, ms)

    def _didl(self, entries):
        return ('<?xml version="1.0" encoding="utf-8"?>'
                '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" '
                'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
                'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">'
                + "".join(entries)
                + "</DIDL-Lite>")

    def _browse(self, oid, flag, start, count):
        flag = (flag or "").lower()
        if flag == "browsemetadata":
            oid = oid or "0"
            if oid == "0":
                kids = self._children("0")
                if kids is None:
                    return None, 0, 0
                child_count = len(kids)
                didl = self._didl([
                    '<container id="0" parentID="-1" restricted="1" childCount="%d">'
                    "<dc:title>%s</dc:title>"
                    "<upnp:class>object.container.storageFolder</upnp:class>"
                    "</container>" % (child_count, xml_escape(self.friendly_name))])
                return didl, 1, 1
            if oid.startswith("C:"):
                entries = self._children(oid)
                if entries is None:
                    return None, 0, 0
                kids = self._children(oid)
                didl = self._didl([
                    '<container id="%s" parentID="%s" restricted="1" childCount="%d">'
                    "<dc:title>%s</dc:title>"
                    "<upnp:class>object.container.storageFolder</upnp:class>"
                    "</container>" % (
                        xml_escape(oid),
                        xml_escape("0" if "/" not in oid[2:] else "C:" + oid[2:].rsplit("/", 1)[0]),
                        len(kids), xml_escape(oid[2:].split("/")[-1]))])
                return didl, 1, 1
            if oid.startswith("I:"):
                rel = oid[2:].replace("/", os.sep)
                full = safe_join(self.source_dir, rel)
                if not full or not os.path.isfile(full):
                    return None, 0, 0
                name = os.path.basename(rel)
                didl = self._didl([self._item_entry(rel, name)])
                return didl, 1, 1
            return None, 0, 0

        entries = self._children(oid)
        if entries is None:
            return None, 0, 0
        total = len(entries)
        if count == 0:
            count = total
        page = entries[start:start + count]
        return self._didl(page), len(page), total
