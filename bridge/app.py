"""
Boom Box Bridge
===============

Bringt die 6 Preset-Tasten an SoundTouch-Lautsprechern zurück, ganz ohne Bose-Cloud.

Ablauf:
  Taste am Lautsprecher  ->  WebSocket-Ereignis (ws://<ip>:8080, Protokoll "gabbo")
                         ->  Bridge sucht den Sender für diese Taste
                         ->  UPnP SetAVTransportURI + Play (http://<ip>:8091/AVTransport/Control)
                         ->  Lautsprecher holt den Stream direkt beim Radiosender

Die Bridge findet Lautsprecher per Bonjour/mDNS (_soundtouch._tcp) und optional über
feste IP-Adressen (Umgebungsvariable SPEAKERS). Belegungen werden in DATA_DIR/presets.json
gespeichert und über eine kleine Weboberfläche und eine HTTP-API verwaltet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

VERSION = "1.1.2"

# ---------------------------------------------------------------------------
# Einstellungen (Umgebungsvariablen)
# ---------------------------------------------------------------------------

BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATIC_DIR = Path(__file__).parent / "static"
STATIC_SPEAKERS = [s.strip() for s in os.environ.get("SPEAKERS", "").split(",") if s.strip()]
DISCOVERY = os.environ.get("DISCOVERY", "on").lower() not in ("off", "0", "false", "no")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Ports des Lautsprechers (nur zum Testen ändern)
API_PORT = int(os.environ.get("SPEAKER_API_PORT", "8090"))
WS_PORT = int(os.environ.get("SPEAKER_WS_PORT", "8080"))
UPNP_PORT = int(os.environ.get("SPEAKER_UPNP_PORT", "8091"))

PRESET_COUNT = 6
DEBOUNCE_SECONDS = 2.0
# Automatisches Fortsetzen nach "INVALID_SOURCE" (z. B. nach Aus/Ein)
AUTO_RESUME = os.environ.get("AUTO_RESUME", "on").lower() not in ("off", "0", "false", "no")
RESUME_COOLDOWN_SECONDS = 20.0
RESUME_MAX_ATTEMPTS = 3          # innerhalb von RESUME_WINDOW_SECONDS
RESUME_WINDOW_SECONDS = 300.0

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)-7s %(message)s")
log = logging.getLogger("bridge")


# ---------------------------------------------------------------------------
# Datenmodell
# ---------------------------------------------------------------------------

@dataclass
class Station:
    name: str
    url: str
    codec: str = ""
    logo: str = ""

    @property
    def mime(self) -> str:
        return "audio/aac" if "AAC" in self.codec.upper() else "audio/mpeg"

    @staticmethod
    def from_dict(d: Optional[dict]) -> Optional["Station"]:
        if not d or not isinstance(d, dict):
            return None
        name = str(d.get("name", "")).strip()
        url = str(d.get("url", "")).strip()
        if not name or not url.lower().startswith(("http://", "https://")):
            return None
        return Station(name=name[:120], url=url[:2000],
                       codec=str(d.get("codec", ""))[:20], logo=str(d.get("logo", ""))[:2000])


@dataclass
class Speaker:
    device_id: str
    host: str
    name: str = ""
    model: str = ""
    connected: bool = False
    last_event: float = 0.0
    last_preset: int = 0
    last_error: str = ""
    resume_attempts: list = field(default_factory=list, repr=False)
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "deviceId": self.device_id, "host": self.host, "name": self.name, "model": self.model,
            "connected": self.connected, "lastPreset": self.last_preset,
            "lastEvent": self.last_event, "lastError": self.last_error,
        }


class PresetStore:
    """Belegungen pro Lautsprecher (deviceID) plus "*" als Standard für alle."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, list[Optional[dict]]] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            for key, slots in raw.get("presets", {}).items():
                self.data[key] = self._normalise(slots)
        except FileNotFoundError:
            pass
        except Exception as exc:  # beschädigte Datei nicht überschreiben
            log.error("presets.json konnte nicht gelesen werden: %s", exc)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "presets": self.data}, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _normalise(slots) -> list[Optional[dict]]:
        out: list[Optional[dict]] = []
        for i in range(PRESET_COUNT):
            item = slots[i] if isinstance(slots, list) and i < len(slots) else None
            st = Station.from_dict(item)
            out.append(asdict(st) if st else None)
        return out

    def get(self, device_id: str) -> list[Optional[dict]]:
        return self.data.get(device_id) or [None] * PRESET_COUNT

    def set(self, device_id: str, slots) -> list[Optional[dict]]:
        self.data[device_id] = self._normalise(slots)
        self.save()
        return self.data[device_id]

    def resolve(self, device_id: str, slot: int) -> Optional[Station]:
        """Sender für Taste `slot` (1-6): zuerst Lautsprecher-eigene Belegung, dann Standard."""
        for key in (device_id, "*"):
            slots = self.data.get(key)
            if slots and 1 <= slot <= PRESET_COUNT and slots[slot - 1]:
                return Station.from_dict(slots[slot - 1])
        return None


# ---------------------------------------------------------------------------
# Kommunikation mit dem Lautsprecher
# ---------------------------------------------------------------------------

def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_preset_press(xml_text: str) -> Optional[int]:
    """Erkennt einen Tastendruck in einem WebSocket-Ereignis.

    Beispiel:
      <updates deviceID="..."><nowSelectionUpdated><preset id="3">...</preset></nowSelectionUpdated></updates>
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for el in root.iter():
        if local_name(el.tag) == "nowSelectionUpdated":
            for child in el.iter():
                if local_name(child.tag) == "preset":
                    try:
                        slot = int(child.attrib.get("id", "0"))
                    except ValueError:
                        return None
                    return slot if 1 <= slot <= PRESET_COUNT else None
    return None


def parse_now_playing(xml_text: str) -> Optional[dict]:
    """Liest ein nowPlayingUpdated-Ereignis aus.

    Liefert {"source": ..., "location": ..., "name": ...} oder None.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for el in root.iter():
        if local_name(el.tag) != "nowPlayingUpdated":
            continue
        for np in el.iter():
            if local_name(np.tag) != "nowPlaying":
                continue
            info = {"source": np.attrib.get("source", ""), "location": "", "name": ""}
            for child in np.iter():
                tag = local_name(child.tag)
                if tag == "ContentItem":
                    info["location"] = info["location"] or child.attrib.get("location", "")
                    info["source"] = info["source"] or child.attrib.get("source", "")
                elif tag in ("itemName", "stationName") and child.text and not info["name"]:
                    info["name"] = child.text.strip()
            return info
    return None


def didl_for(station: Station) -> str:
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="boombox" parentID="0" restricted="1">'
        f"<dc:title>{escape(station.name)}</dc:title>"
        "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
        f'<res protocolInfo="http-get:*:{station.mime}:*">{escape(station.url)}</res>'
        "</item></DIDL-Lite>"
    )


class SpeakerClient:
    def __init__(self, session: ClientSession):
        self.session = session

    async def info(self, host: str) -> tuple[str, str, str]:
        async with self.session.get(f"http://{host}:{API_PORT}/info") as resp:
            resp.raise_for_status()
            root = ET.fromstring(await resp.text())
        device_id = root.attrib.get("deviceID", host)
        name = root.findtext("name") or host
        model = root.findtext("type") or "SoundTouch"
        return device_id, name, model

    async def soap(self, host: str, action: str, args: str) -> None:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            f"{args}</u:{action}></s:Body></s:Envelope>"
        )
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
        }
        url = f"http://{host}:{UPNP_PORT}/AVTransport/Control"
        async with self.session.post(url, data=body.encode("utf-8"), headers=headers) as resp:
            if resp.status >= 300:
                text = await resp.text()
                detail = ""
                try:
                    root = ET.fromstring(text)
                    for el in root.iter():
                        if local_name(el.tag) in ("errorDescription", "faultstring") and el.text:
                            detail = el.text
                            break
                except ET.ParseError:
                    pass
                raise RuntimeError(f"UPnP {action} fehlgeschlagen (HTTP {resp.status}) {detail}".strip())

    @staticmethod
    def candidates(station: Station) -> list[tuple[str, str]]:
        """Varianten: Original, ggf. HTTP statt HTTPS, dann allgemeinere Medientypen."""
        urls = [station.url]
        if station.url.lower().startswith("https://"):
            urls.append("http://" + station.url[8:])
        mimes = [station.mime] + [m for m in ("audio/mpeg", "*") if m != station.mime]
        return [(u, m) for u in urls for m in mimes]

    async def play(self, host: str, station: Station) -> None:
        """Spielt einen Stream ab. Der Lautsprecher lehnt manche Kombinationen aus Adresse und
        Medientyp mit "Can't play, no URI supplied" ab, darum werden Varianten probiert."""
        last_exc: Optional[Exception] = None
        for attempt, (url, mime) in enumerate(self.candidates(station)):
            variant = Station(name=station.name, url=url, codec=station.codec, logo=station.logo)
            didl = didl_for(variant).replace(f"http-get:*:{variant.mime}:*", f"http-get:*:{mime}:*")
            set_uri = ("<InstanceID>0</InstanceID>"
                       f"<CurrentURI>{escape(url)}</CurrentURI>"
                       f"<CurrentURIMetaData>{escape(didl)}</CurrentURIMetaData>")
            try:
                # Nach Aus/Ein braucht der Lautsprecher oft zuerst ein Stop.
                try:
                    await self.soap(host, "Stop", "<InstanceID>0</InstanceID>")
                except Exception:
                    pass
                await self.soap(host, "SetAVTransportURI", set_uri)
                await asyncio.sleep(0.4)
                await self.soap(host, "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
                if attempt:
                    log.info("Abspielen auf %s klappte mit Variante %s (%s)", host, url, mime)
                return
            except Exception as exc:
                last_exc = exc
                log.info("Abspielen auf %s, Variante %d (%s, %s) fehlgeschlagen: %s",
                         host, attempt + 1, url, mime, exc)
        raise last_exc


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class Bridge:
    def __init__(self):
        self.store = PresetStore(DATA_DIR / "presets.json")
        self.last_path = DATA_DIR / "last_played.json"
        self.last_played: dict[str, dict] = self._load_last()
        self.speakers: dict[str, Speaker] = {}
        self.session: Optional[ClientSession] = None
        self.client: Optional[SpeakerClient] = None
        self.started = time.time()
        self._last_press: dict[tuple[str, int], float] = {}
        self._zc = None
        self._browser = None

    # --- Zuletzt gespielter Sender --------------------------------------

    def _load_last(self) -> dict:
        try:
            return json.loads(self.last_path.read_text("utf-8"))
        except Exception:
            return {}

    def remember(self, device_id: str, station: Station) -> None:
        current = self.last_played.get(device_id)
        if current and current.get("url") == station.url and current.get("name") == station.name:
            return
        self.last_played[device_id] = asdict(station)
        try:
            self.last_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.last_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.last_played, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self.last_path)
        except Exception as exc:
            log.warning("last_played.json konnte nicht gespeichert werden: %s", exc)

    # --- Lebenszyklus ---------------------------------------------------

    async def start(self, app: web.Application) -> None:
        self.session = ClientSession(timeout=ClientTimeout(total=8))
        self.client = SpeakerClient(self.session)
        for host in STATIC_SPEAKERS:
            asyncio.create_task(self.add_host(host))
        if DISCOVERY:
            await self._start_discovery()
        app["rescan"] = asyncio.create_task(self._rescan_loop())
        log.info("Boom Box Bridge %s läuft auf Port %d (Discovery: %s, feste IPs: %s)",
                 VERSION, BRIDGE_PORT, "an" if DISCOVERY else "aus", ", ".join(STATIC_SPEAKERS) or "-")

    async def stop(self, app: web.Application) -> None:
        app["rescan"].cancel()
        for sp in self.speakers.values():
            if sp.task:
                sp.task.cancel()
        if self._zc:
            await self._zc.async_close()
        if self.session:
            await self.session.close()

    # --- Lautsprecher verwalten ----------------------------------------

    async def add_host(self, host: str) -> Optional[Speaker]:
        try:
            device_id, name, model = await self.client.info(host)
        except Exception as exc:
            log.debug("Kein SoundTouch unter %s: %s", host, exc)
            return None

        sp = self.speakers.get(device_id)
        if sp is None:
            sp = Speaker(device_id=device_id, host=host, name=name, model=model)
            self.speakers[device_id] = sp
            log.info("Lautsprecher gefunden: %s (%s) auf %s", name, model, host)
        else:
            if sp.host != host:
                log.info("%s hat neue IP %s (vorher %s)", name, host, sp.host)
                sp.host = host
                if sp.task:
                    sp.task.cancel()
                    sp.task = None
            sp.name, sp.model = name, model

        if sp.task is None or sp.task.done():
            sp.task = asyncio.create_task(self._listen(sp))
        return sp

    async def _rescan_loop(self) -> None:
        while True:
            await asyncio.sleep(120)
            for sp in list(self.speakers.values()):
                await self.add_host(sp.host)
            for host in STATIC_SPEAKERS:
                await self.add_host(host)

    # --- mDNS ----------------------------------------------------------

    async def _start_discovery(self) -> None:
        try:
            from zeroconf import ServiceStateChange, IPVersion
            from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo
        except ImportError:
            log.warning("zeroconf nicht installiert, nur feste IPs werden verwendet")
            return

        self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        loop = asyncio.get_running_loop()

        async def resolve(zc, type_, name):
            info = AsyncServiceInfo(type_, name)
            if await info.async_request(zc, 3000):
                for addr in info.parsed_addresses(IPVersion.V4Only):
                    await self.add_host(addr)

        def on_change(zeroconf, service_type, name, state_change):
            if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
                loop.create_task(resolve(zeroconf, service_type, name))

        self._browser = AsyncServiceBrowser(self._zc.zeroconf, ["_soundtouch._tcp.local."], handlers=[on_change])

        # Die Bridge selbst ankündigen, damit die App sie findet.
        try:
            ip = self._local_ip()
            info = AsyncServiceInfo(
                "_boombox-bridge._tcp.local.",
                f"Boom Box Bridge ({socket.gethostname()})._boombox-bridge._tcp.local.",
                addresses=[socket.inet_aton(ip)], port=BRIDGE_PORT,
                properties={"version": VERSION, "api": "1"},
            )
            await self._zc.async_register_service(info)
            log.info("Bridge per Bonjour angekündigt auf %s:%d", ip, BRIDGE_PORT)
        except Exception as exc:
            log.warning("Bonjour-Ankündigung fehlgeschlagen: %s", exc)

    @staticmethod
    def _local_ip() -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    # --- WebSocket pro Lautsprecher ------------------------------------

    async def _listen(self, sp: Speaker) -> None:
        delay = 2
        while True:
            url = f"ws://{sp.host}:{WS_PORT}/"
            try:
                async with self.session.ws_connect(url, protocols=("gabbo",), heartbeat=30,
                                                   timeout=ClientTimeout(total=None, connect=8)) as ws:
                    sp.connected, sp.last_error, delay = True, "", 2
                    log.info("Verbunden mit %s (%s)", sp.name, sp.host)
                    async for msg in ws:
                        if msg.type == WSMsgType.TEXT:
                            await self.handle_event(sp, msg.data)
                        elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                            break
            except asyncio.CancelledError:
                sp.connected = False
                raise
            except Exception as exc:
                sp.last_error = str(exc) or exc.__class__.__name__
                log.debug("WebSocket %s: %s", sp.host, sp.last_error)
            sp.connected = False
            log.info("Verbindung zu %s getrennt, neuer Versuch in %ds", sp.name, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def handle_event(self, sp: Speaker, text: str) -> None:
        log.debug("Ereignis von %s: %s", sp.name, text[:500])

        np = parse_now_playing(text)
        if np is not None:
            await self.handle_now_playing(sp, np)

        slot = parse_preset_press(text)
        if slot is None:
            return
        key = (sp.device_id, slot)
        now = time.monotonic()
        if now - self._last_press.get(key, 0) < DEBOUNCE_SECONDS:
            return
        self._last_press[key] = now
        sp.last_preset, sp.last_event = slot, time.time()

        station = self.store.resolve(sp.device_id, slot)
        if station is None:
            log.info("%s: Taste %d gedrückt, aber nicht belegt", sp.name, slot)
            return
        log.info("%s: Taste %d -> %s", sp.name, slot, station.name)
        await self.play(sp, station)

    async def handle_now_playing(self, sp: Speaker, np: dict) -> None:
        source = np.get("source", "").upper()

        # Merken, was gerade läuft (auch wenn es aus der App gestartet wurde).
        location = np.get("location", "")
        if source == "UPNP" and location.lower().startswith(("http://", "https://")):
            name = np.get("name") or self.last_played.get(sp.device_id, {}).get("name") or "Radio"
            prev = self.last_played.get(sp.device_id, {})
            codec = prev.get("codec", "") if prev.get("url") == location else ""
            self.remember(sp.device_id, Station(name=name, url=location, codec=codec))
            return

        if source != "INVALID_SOURCE" or not AUTO_RESUME:
            return
        last = Station.from_dict(self.last_played.get(sp.device_id))
        if last is None:
            log.info("%s: INVALID_SOURCE, aber kein zuletzt gespielter Sender bekannt", sp.name)
            return

        now = time.monotonic()
        sp.resume_attempts = [t for t in sp.resume_attempts if now - t < RESUME_WINDOW_SECONDS]
        if sp.resume_attempts and now - sp.resume_attempts[-1] < RESUME_COOLDOWN_SECONDS:
            return
        if len(sp.resume_attempts) >= RESUME_MAX_ATTEMPTS:
            log.warning("%s: Fortsetzen von %s mehrfach fehlgeschlagen, versuche es vorerst nicht mehr",
                        sp.name, last.name)
            return
        sp.resume_attempts.append(now)

        log.info("%s: INVALID_SOURCE -> spiele zuletzt gehörten Sender %s", sp.name, last.name)
        await asyncio.sleep(1.0)  # dem Lautsprecher kurz Zeit nach dem Einschalten geben
        try:
            await self.play(sp, last)
        except Exception:
            pass

    async def play(self, sp: Speaker, station: Station) -> None:
        try:
            await self.client.play(sp.host, station)
            self.remember(sp.device_id, station)
            sp.last_error = ""
        except Exception as exc:
            sp.last_error = str(exc)
            log.error("%s: Abspielen von %s fehlgeschlagen: %s", sp.name, station.name, exc)
            raise


# ---------------------------------------------------------------------------
# HTTP-API und Weboberfläche
# ---------------------------------------------------------------------------

def json_response(data, status=200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def build_app(bridge: Bridge) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/api/status")
    async def status(_):
        return json_response({"version": VERSION, "uptime": int(time.time() - bridge.started),
                              "speakers": [sp.public() for sp in bridge.speakers.values()]})

    @routes.get("/api/speakers")
    async def speakers(_):
        return json_response([
            {**sp.public(), "presets": bridge.store.get(sp.device_id)}
            for sp in sorted(bridge.speakers.values(), key=lambda s: s.name.lower())
        ])

    @routes.post("/api/speakers")
    async def add_speaker(request):
        body = await request.json()
        host = str(body.get("host", "")).strip()
        if not host:
            return json_response({"error": "host fehlt"}, 400)
        sp = await bridge.add_host(host)
        if sp is None:
            return json_response({"error": f"Kein SoundTouch unter {host} gefunden"}, 404)
        return json_response(sp.public())

    @routes.get("/api/presets/{device_id}")
    async def get_presets(request):
        return json_response(bridge.store.get(request.match_info["device_id"]))

    @routes.put("/api/presets/{device_id}")
    async def put_presets(request):
        device_id = request.match_info["device_id"]
        if device_id != "*" and device_id not in bridge.speakers:
            return json_response({"error": "Unbekannter Lautsprecher"}, 404)
        body = await request.json()
        slots = body.get("presets") if isinstance(body, dict) else body
        return json_response(bridge.store.set(device_id, slots))

    @routes.post("/api/play/{device_id}")
    async def play(request):
        sp = bridge.speakers.get(request.match_info["device_id"])
        if sp is None:
            return json_response({"error": "Unbekannter Lautsprecher"}, 404)
        body = await request.json()
        station = None
        if "slot" in body:
            station = bridge.store.resolve(sp.device_id, int(body["slot"]))
        else:
            station = Station.from_dict(body)
        if station is None:
            return json_response({"error": "Kein gültiger Sender"}, 400)
        try:
            await bridge.play(sp, station)
        except Exception as exc:
            return json_response({"error": str(exc)}, 502)
        return json_response({"ok": True, "station": asdict(station)})

    @routes.get("/")
    async def index(_):
        return web.FileResponse(STATIC_DIR / "index.html")

    app = web.Application(client_max_size=256 * 1024)
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC_DIR)
    app.on_startup.append(bridge.start)
    app.on_cleanup.append(bridge.stop)
    return app


def main() -> None:
    bridge = Bridge()
    web.run_app(build_app(bridge), host="0.0.0.0", port=BRIDGE_PORT, print=None)


if __name__ == "__main__":
    main()
