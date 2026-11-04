import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from conftest import HAS_FFMPEG, make_audio
from otokura import dlna

DESC = """<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType><friendlyName>書斎のプレーヤー</friendlyName>
<manufacturer>蔵</manufacturer><modelName>NP-1</modelName><UDN>uuid:abc</UDN><serviceList>
<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/avt</controlURL></service>
<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType><controlURL>/rc</controlURL></service>
</serviceList></device></root>"""


class Fake:
    """DLNA の機器のふり。受けた操作を覚えておく。"""

    def __init__(self):
        fake = self
        self.actions, self.uri, self.meta, self.state, self.vol, self.pos, self.reject = [], "", "", "STOPPED", 30, "0:00:00", None

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = DESC.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
                action = self.headers["SOAPAction"].strip('"').split("#")[1]
                fake.actions.append(action)
                if action == fake.reject:
                    return self.reply(500, "<s:Fault><detail><UPnPError><errorCode>714</errorCode></UPnPError></detail></s:Fault>")
                out = ""
                if action == "SetAVTransportURI":
                    fake.uri = raw.split("<CurrentURI>")[1].split("</CurrentURI>")[0]
                    fake.meta = raw.split("<CurrentURIMetaData>")[1].split("</CurrentURIMetaData>")[0]
                elif action == "Play":
                    fake.state = "PLAYING"
                elif action == "Pause":
                    fake.state = "PAUSED_PLAYBACK"
                elif action == "Stop":
                    fake.state = "STOPPED"
                elif action == "Seek":
                    fake.pos = raw.split("<Target>")[1].split("</Target>")[0]
                elif action == "SetVolume":
                    fake.vol = int(raw.split("<DesiredVolume>")[1].split("<")[0])
                elif action == "GetTransportInfo":
                    out = f"<CurrentTransportState>{fake.state}</CurrentTransportState>"
                elif action == "GetPositionInfo":
                    out = f"<RelTime>{fake.pos}</RelTime><TrackDuration>0:03:00</TrackDuration><TrackURI>{fake.uri}</TrackURI>"
                elif action == "GetVolume":
                    out = f"<CurrentVolume>{fake.vol}</CurrentVolume>"
                self.reply(200, f'<u:{action}Response xmlns:u="x">{out}</u:{action}Response>')

            def reply(self, code, inner):
                body = f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>{inner}</s:Body></s:Envelope>'.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.location = f"http://127.0.0.1:{self.http.server_port}/desc.xml"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)    # M-SEARCH に答える
        self.udp.bind(("127.0.0.1", 0))
        self.ssdp_port = self.udp.getsockname()[1]
        self.searches = []
        threading.Thread(target=self._ssdp, daemon=True).start()

    def _ssdp(self):
        while True:
            try:
                data, addr = self.udp.recvfrom(2048)
            except OSError:
                return
            self.searches.append(data.decode())
            self.udp.sendto((f"HTTP/1.1 200 OK\r\nST: {dlna.ST}\r\nLOCATION: {self.location}\r\n\r\n").encode(), addr)

    def close(self):
        self.http.shutdown()
        self.udp.close()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.close()


def test_ssdp_and_helpers():
    assert dlna.parse_ssdp(f"HTTP/1.1 200 OK\r\nST: {dlna.ST}\r\nLocation: http://x/d.xml\r\n\r\n".encode()) == "http://x/d.xml"
    assert dlna.parse_ssdp(b"HTTP/1.1 200 OK\r\nST: urn:schemas-upnp-org:device:MediaServer:1\r\nLOCATION: http://x/\r\n\r\n") is None
    assert dlna.parse_ssdp(b"NOTIFY * HTTP/1.1\r\nLOCATION: http://x/\r\n\r\n") is None
    assert dlna.hms(3725) == "1:02:05" and dlna.parse_hms("0:03:20.500") == 200.5 and dlna.parse_hms("NOT_IMPLEMENTED") is None
    assert dlna.is_private_url("http://127.0.0.1:1/") and not dlna.is_private_url("http://8.8.8.8/") and not dlna.is_private_url("file:///etc/passwd")


def test_description_rules():
    r = dlna.parse_description(DESC.encode(), "http://10.0.0.5/d.xml")
    assert r.name == "書斎のプレーヤー" and r.model == "蔵 NP-1" and r.avt_url == "http://10.0.0.5/avt" and r.rc_url == "http://10.0.0.5/rc"
    # 説明ファイルが別のホストを指していたら操作を送らない
    evil = DESC.replace("<controlURL>/avt</controlURL>", "<controlURL>http://169.254.169.254/x</controlURL>").encode()
    assert dlna.parse_description(evil, "http://10.0.0.5/d.xml") is None
    with pytest.raises(dlna.DlnaError):
        dlna.parse_description(b'<!DOCTYPE x [<!ENTITY a "b">]><root/>', "http://10.0.0.5/")
    server = DESC.replace("AVTransport", "ContentDirectory").encode()
    assert dlna.parse_description(server, "http://10.0.0.5/") is None


def test_didl_escapes():
    x = dlna.didl("http://n/a?x=1&y=2", "audio/flac", "A&B <夜>", "C", None, None, 200000)
    assert "A&amp;B &lt;夜&gt;" in x and "x=1&amp;y=2" in x and 'duration="0:03:20.000"' in x and "upnp:album>" not in x


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg が必要")
def test_control_through_api(tmp_path, fake, monkeypatch):
    from otokura.app import create_app
    from otokura.config import Config, Source
    make_audio(tmp_path / "m" / "a" / "song & one.flac")
    make_audio(tmp_path / "m" / "a" / "hires.tta", codec_args=("-c:a", "tta"))
    cfg = Config(sources=[Source("m", "M", tmp_path / "m")], data_dir=tmp_path / "data", scan_at=None, scan_on_start=False,
                 lrclib=False, webinfo=False, cast_url="http://192.168.0.252:8686")
    app = create_app(cfg, start_background=False)
    app.state.scanner.run()
    app.state.dlna.hosts, app.state.dlna.port = ["127.0.0.1"], fake.ssdp_port
    c = TestClient(app)
    j = c.get("/api/library").json()
    t = {r[j["trackColumns"].index("codec")]: r[0] for r in j["tracks"]}

    d = c.get("/api/dlna/devices?search=1").json()
    assert d["castUrl"] == "http://192.168.0.252:8686" and [x["name"] for x in d["devices"]] == ["書斎のプレーヤー"]
    did = d["devices"][0]["id"]
    assert any("M-SEARCH" in s for s in fake.searches)

    r = c.post(f"/api/dlna/{did}/load", json={"trackId": t["FLAC"], "pos": 0}).json()
    assert r["uri"] == f"http://192.168.0.252:8686/api/tracks/{t['FLAC']}/stream?cast=1&dlna=1" and r["mime"] == "audio/flac"
    assert fake.actions[-3:] == ["Stop", "SetAVTransportURI", "Play"] and fake.uri.endswith("cast=1&amp;dlna=1")
    assert "song &amp;amp; one" in fake.meta      # DIDL の中の曲名は、SOAP に入れるときにもう一度エスケープされる
    assert c.get(f"/api/dlna/{did}/status").json() == {"state": "PLAYING", "pos": 0, "duration": 180, "uri": r["uri"], "volume": 30}

    c.post(f"/api/dlna/{did}/seek", json={"pos": 65})
    assert fake.pos == "0:01:05"
    c.post(f"/api/dlna/{did}/pause")
    assert fake.state == "PAUSED_PLAYBACK"
    c.post(f"/api/dlna/{did}/volume", json={"volume": 55})
    assert fake.vol == 55
    c.post(f"/api/dlna/{did}/stop")
    assert fake.state == "STOPPED"

    # 途中から: 再生してからシークする。変換が要る形式は渡す前に変換しておく
    fake.actions.clear()
    r = c.post(f"/api/dlna/{did}/load", json={"trackId": t["TTA"], "pos": 2}).json()
    assert r["mime"] == "audio/flac" and fake.actions[-2:] == ["Play", "Seek"]

    # 機器が断ったらエラーを画面に返す
    fake.reject = "SetAVTransportURI"
    res = c.post(f"/api/dlna/{did}/load", json={"trackId": t["FLAC"]})
    assert res.status_code == 502 and "714" in res.json()["detail"]
    fake.reject = None

    # 機器が曲を取りに来る: HEAD と DLNA の応答ヘッダー、Range
    res = c.head(f"/api/tracks/{t['FLAC']}/stream?cast=1&dlna=1")
    assert res.status_code == 200 and res.headers["transferMode.dlna.org"] == "Streaming" and "DLNA.ORG_OP=01" in res.headers["contentFeatures.dlna.org"]
    res = c.get(f"/api/tracks/{t['FLAC']}/stream?cast=1&dlna=1", headers={"Range": "bytes=0-99"})
    assert res.status_code == 206 and len(res.content) == 100


def test_add_by_ip_finds_device_that_answers_only_multicast(tmp_path, fake, monkeypatch):
    # foobar2000 Renderer などは、マルチキャストの M-SEARCH にしか返事をしない。IP の手入力でも見つけられるように
    from otokura.db import Database
    calls = []

    def only_multicast(hosts=None, timeout=3.0, port=dlna.SSDP_PORT, multicast=True):
        calls.append(multicast)
        return [fake.location, "http://192.168.0.99:1/other.xml"] if multicast else []
    monkeypatch.setattr(dlna, "discover", only_multicast)
    d = dlna.Dlna(Database(tmp_path / "d.db"))
    assert d.add("127.0.0.1").name == "書斎のプレーヤー" and calls[0] is True
    with pytest.raises(dlna.DlnaError):
        d.add("192.168.0.200")                      # 返事の中に無いアドレスは足さない


def test_search_uses_multicast_and_sends_twice(monkeypatch):
    # UDP は落ちることがあるので 2 回送る。同じネットワークの全アドレスへの直接送信はしない
    sent = []

    class S:
        def setsockopt(self, *a): pass
        def settimeout(self, *a): pass
        def sendto(self, data, addr): sent.append(addr)
        def recvfrom(self, n): raise socket.timeout
        def close(self): pass
    monkeypatch.setattr(dlna.socket, "socket", lambda *a: S())
    assert dlna.discover(timeout=0.01) == []
    assert sent == [(dlna.SSDP_ADDR, dlna.SSDP_PORT)] * 2


def test_manual_add_and_remove(tmp_path, fake):
    from otokura.db import Database
    d = dlna.Dlna(Database(tmp_path / "d.db"), port=fake.ssdp_port)
    d.hosts = ["127.0.0.1"]
    assert d.add(fake.location).manual
    assert list(d.devices.values())[0].name == "書斎のプレーヤー"
    d2 = dlna.Dlna(d.db, port=fake.ssdp_port)      # 再起動しても手で足した機器は残る
    d2.hosts = ["127.0.0.1"]
    d2.search()
    did = next(iter(d2.devices))
    assert d2.devices[did].manual
    d2.remove(did)
    assert d2.devices == {} and d2._manual() == []
    with pytest.raises(dlna.DlnaError):
        d2.add("not an address")
    with pytest.raises(dlna.DlnaError):
        d2.add("http://8.8.8.8/d.xml")             # 家の外には行かない
    assert d2.add(f"127.0.0.1:{fake.ssdp_port}").name == "書斎のプレーヤー"      # IP アドレスだけでも足せる
