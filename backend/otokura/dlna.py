"""DLNA・UPnP の機器(ネットワークプレーヤー、AV アンプなど)を NAS から操作する。

- 探す: SSDP の M-SEARCH。マルチキャストに加えて同じネットワークの各アドレスへ直接送る
  (Docker のブリッジではマルチキャストが外へ出ないが、直接送れば機器は返事をくれる)。IP アドレスの手入力もできる
- 操作: AVTransport と RenderingControl の SOAP。機器が曲を NAS から取りに来るので、キューは画面が持つ(Cast と同じ)
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape

import httpx

log = logging.getLogger("otokura.dlna")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
TIMEOUT_S = 6.0
MAX_XML_BYTES = 512 * 1024
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class DlnaError(Exception):
    """機器が操作を受け付けなかった・返事が無い。"""


def msearch(host: str = SSDP_ADDR, port: int = SSDP_PORT) -> bytes:
    return (f'M-SEARCH * HTTP/1.1\r\nHOST: {host}:{port}\r\nMAN: "ssdp:discover"\r\nMX: 2\r\nST: {ST}\r\n'
            'USER-AGENT: Otokura/1 UPnP/1.1\r\n\r\n').encode("ascii")


def parse_ssdp(data: bytes) -> str | None:
    """SSDP の返事から LOCATION を取り出す。MediaRenderer 以外は None。"""
    lines = data.decode("utf-8", "replace").split("\r\n")
    if not lines or " 200 " not in lines[0] + " ":
        return None
    head = {}
    for line in lines[1:]:
        k, sep, v = line.partition(":")
        if sep:
            head[k.strip().lower()] = v.strip()
    st = head.get("st", "")
    if st and "MediaRenderer" not in st and st not in ("upnp:rootdevice", "ssdp:all"):
        return None
    return head.get("location") or None


def subnet_hosts(ip: str | None) -> list[str]:
    """同じ /24 のアドレス。プライベートアドレスのときだけ。"""
    try:
        a = ipaddress.ip_address(ip or "")
    except ValueError:
        return []
    if a.version != 4 or not a.is_private or a.is_loopback:
        return []
    return [str(h) for h in ipaddress.ip_network(f"{a}/24", strict=False).hosts() if h != a]


def discover(hosts: list[str] | None = None, timeout: float = 2.5, port: int = SSDP_PORT, multicast: bool = True) -> list[str]:
    """機器の説明ファイルの URL を集める。"""
    found: list[str] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.2)
        if multicast:
            try:
                s.sendto(msearch(), (SSDP_ADDR, SSDP_PORT))
            except OSError as e:
                log.debug("マルチキャストを送れません: %s", e)
        for h in hosts or []:
            try:
                s.sendto(msearch(h, port), (h, port))
            except OSError:
                pass
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            loc = parse_ssdp(data)
            if loc and loc not in found:
                found.append(loc)
    finally:
        s.close()
    return found


def is_private_url(url: str) -> bool:
    """家の中の機器だけを相手にする(手入力や SSDP の返事で外のアドレスへ行かない)。"""
    u = urlparse(url)
    if u.scheme != "http" or not u.hostname:
        return False
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or 80, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_private for i in infos)


def _parse_xml(data: bytes) -> ET.Element:
    if len(data) > MAX_XML_BYTES or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise DlnaError("機器の返事を読めません")
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise DlnaError("機器の返事を読めません") from e


def _iter(el: ET.Element, name: str):
    """名前空間を問わず探す(iter は {*} を受け付けない)。"""
    return (e for e in el.iter() if e.tag.rsplit("}", 1)[-1] == name)


def _text(el: ET.Element, name: str) -> str:
    return (el.findtext(f"{{*}}{name}") or "").strip()


def hms(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_hms(v: str | None) -> float | None:
    m = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2})(\.\d+)?", (v or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + float(m.group(4) or 0)


def didl(url: str, mime: str, title: str, artist: str | None, album: str | None, art: str | None, duration_ms: int | None) -> str:
    c = lambda s: escape(_CTRL.sub("", s or ""))
    dur = f' duration="{hms((duration_ms or 0) / 1000)}.000"' if duration_ms else ""
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"><item id="1" parentID="0" restricted="1">'
            f"<dc:title>{c(title)}</dc:title>"
            + (f"<upnp:artist>{c(artist)}</upnp:artist><dc:creator>{c(artist)}</dc:creator>" if artist else "")
            + (f"<upnp:album>{c(album)}</upnp:album>" if album else "")
            + (f"<upnp:albumArtURI>{c(art)}</upnp:albumArtURI>" if art else "")
            + f'<res protocolInfo="http-get:*:{c(mime)}:*"{dur}>{c(url)}</res>'
            "<upnp:class>object.item.audioItem.musicTrack</upnp:class></item></DIDL-Lite>")


@dataclass
class Renderer:
    id: str
    name: str
    model: str
    location: str
    avt_type: str
    avt_url: str
    rc_type: str | None = None
    rc_url: str | None = None
    manual: bool = False

    @property
    def host(self) -> str:
        return urlparse(self.location).hostname or ""

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "model": self.model, "host": self.host, "manual": self.manual,
                "volume": bool(self.rc_url)}

    def call(self, service: str, url: str, action: str, **args) -> dict:
        body = "".join(f"<{k}>{escape(_CTRL.sub('', str(v)))}</{k}>" for k, v in args.items())
        xml = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
               f's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:{action} xmlns:u="{service}">{body}'
               f"</u:{action}></s:Body></s:Envelope>")
        try:
            r = httpx.post(url, content=xml.encode("utf-8"), timeout=TIMEOUT_S, headers={
                "Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{service}#{action}"'})
        except httpx.HTTPError as e:
            raise DlnaError(f"{self.name} に接続できません") from e
        if r.status_code >= 400:
            code = ""
            try:
                root = _parse_xml(r.content)
                code = next((e.text or "" for e in _iter(root, "errorCode")), "")
            except DlnaError:
                pass
            raise DlnaError(f"{self.name} が {action} を受け付けませんでした" + (f"(エラー {code})" if code else f"(HTTP {r.status_code})"))
        root = _parse_xml(r.content)
        resp = next((e for e in root.iter() if e.tag.endswith(f"{action}Response")), None)
        return {c.tag.rsplit("}", 1)[-1]: (c.text or "") for c in resp} if resp is not None else {}

    def avt(self, action: str, **args) -> dict:
        return self.call(self.avt_type, self.avt_url, action, InstanceID=0, **args)

    def load(self, url: str, metadata: str, position: float = 0, play: bool = True) -> None:
        try:
            self.avt("Stop")
        except DlnaError:
            pass     # 止まっている機器はエラーを返すことがある
        self.avt("SetAVTransportURI", CurrentURI=url, CurrentURIMetaData=metadata)
        if play:
            self.avt("Play", Speed=1)
        if position >= 1:
            for _ in range(5):      # 読み込み中はシークできない機器が多いので少し待ってやり直す
                try:
                    self.seek(position)
                    break
                except DlnaError:
                    time.sleep(0.6)

    def play(self) -> None:
        self.avt("Play", Speed=1)

    def pause(self) -> None:
        self.avt("Pause")

    def stop(self) -> None:
        self.avt("Stop")

    def seek(self, position: float) -> None:
        self.avt("Seek", Unit="REL_TIME", Target=hms(position))

    def volume(self) -> int | None:
        if not self.rc_url or not self.rc_type:
            return None
        try:
            v = self.call(self.rc_type, self.rc_url, "GetVolume", InstanceID=0, Channel="Master").get("CurrentVolume")
            return int(v) if v else None
        except (DlnaError, ValueError):
            return None

    def set_volume(self, value: int) -> None:
        if not self.rc_url or not self.rc_type:
            raise DlnaError(f"{self.name} は音量を操作できません")
        self.call(self.rc_type, self.rc_url, "SetVolume", InstanceID=0, Channel="Master", DesiredVolume=max(0, min(100, int(value))))

    def status(self) -> dict:
        ti = self.avt("GetTransportInfo")
        pi = self.avt("GetPositionInfo")
        return {"state": ti.get("CurrentTransportState") or "UNKNOWN", "pos": parse_hms(pi.get("RelTime")) or 0,
                "duration": parse_hms(pi.get("TrackDuration")) or 0, "uri": pi.get("TrackURI") or "", "volume": self.volume()}


def parse_description(data: bytes, location: str, manual: bool = False) -> Renderer | None:
    """機器の説明ファイル → Renderer。AVTransport が無い機器(サーバーなど)は None。"""
    root = _parse_xml(data)
    base = (root.findtext("{*}URLBase") or "").strip() or location
    loc_host = urlparse(location).hostname
    for dev in _iter(root, "device"):
        avt = rc = None
        for sv in dev.iterfind("{*}serviceList/{*}service"):
            kind = _text(sv, "serviceType")
            ctrl = urljoin(base, _text(sv, "controlURL"))
            if urlparse(ctrl).hostname != loc_host:     # 説明ファイルの書かれた機器以外へは送らない
                continue
            if ":AVTransport:" in kind:
                avt = (kind, ctrl)
            elif ":RenderingControl:" in kind:
                rc = (kind, ctrl)
        if not avt:
            continue
        udn = _text(dev, "UDN") or location
        return Renderer(id=hashlib.sha1(udn.encode("utf-8")).hexdigest()[:12], name=_text(dev, "friendlyName") or loc_host or "DLNA",
                        model=" ".join(x for x in (_text(dev, "manufacturer"), _text(dev, "modelName")) if x),
                        location=location, avt_type=avt[0], avt_url=avt[1], rc_type=rc[0] if rc else None,
                        rc_url=rc[1] if rc else None, manual=manual)
    return None


class Dlna:
    """見つけた機器の一覧。手で足した機器は DB に残す。"""

    def __init__(self, db, lan_ip: str | None = None, port: int = SSDP_PORT):
        self.db, self.lan_ip, self.port = db, lan_ip, port
        self.devices: dict[str, Renderer] = {}
        self._lock = threading.Lock()
        self.searched_at: float | None = None

    def _manual(self) -> list[str]:
        try:
            return json.loads(self.db.meta("dlna_manual") or "[]")
        except ValueError:
            return []

    def _describe(self, location: str, manual: bool = False) -> Renderer | None:
        if not is_private_url(location):
            log.warning("家の中のアドレスではないので無視します: %s", location)
            return None
        try:
            r = httpx.get(location, timeout=TIMEOUT_S, follow_redirects=False)
            r.raise_for_status()
            return parse_description(r.content, location, manual)
        except (httpx.HTTPError, DlnaError) as e:
            log.info("機器の説明を読めません: %s: %s", location, e)
            return None

    def search(self) -> list[Renderer]:
        with self._lock:
            manual = self._manual()
            locations = discover(subnet_hosts(self.lan_ip), port=self.port)
            devs: dict[str, Renderer] = {}
            for loc in locations:
                d = self._describe(loc, loc in manual)
                if d:
                    devs[d.id] = d
            for loc in manual:
                if loc not in locations:
                    d = self._describe(loc, True)
                    if d:
                        devs[d.id] = d
            self.devices = devs
            self.searched_at = time.time()
            return list(devs.values())

    def add(self, address: str) -> Renderer:
        """IP アドレス(そこへ直接 M-SEARCH する)か、説明ファイルの URL で機器を足す。"""
        address = address.strip()
        if address.startswith(("http://", "https://")):
            locations = [address]
        else:
            host, _, port = address.partition(":")
            try:
                ipaddress.ip_address(host)
            except ValueError as e:
                raise DlnaError("IP アドレスか、機器の説明ファイルの URL を入力してください") from e
            locations = discover([host], timeout=2.5, port=int(port) if port.isdigit() else self.port, multicast=False)
            if not locations:
                raise DlnaError(f"{host} から返事がありません。電源と、機器の DLNA の設定を確認してください")
        for loc in locations:
            d = self._describe(loc, True)
            if d:
                with self._lock:
                    manual = self._manual()
                    if loc not in manual:
                        self.db.meta("dlna_manual", json.dumps(manual + [loc]))
                    self.devices[d.id] = d
                return d
        raise DlnaError("再生できる機器ではありません(AVTransport がありません)")

    def remove(self, did: str) -> None:
        with self._lock:
            d = self.devices.pop(did, None)
            if d:
                self.db.meta("dlna_manual", json.dumps([x for x in self._manual() if x != d.location]))

    def get(self, did: str) -> Renderer:
        d = self.devices.get(did)
        if d is None:
            self.search()
            d = self.devices.get(did)
        if d is None:
            raise DlnaError("機器が見つかりません。電源を確認して、もう一度探してください")
        return d
