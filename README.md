<p align="center"><img src="bridge/static/icon.png" width="96" alt=""></p>

<h1 align="center">Boom Box Bridge</h1>

<p align="center">
Brings the 6 preset buttons on SoundTouch speakers back to life, without any cloud.<br>
Bringt die 6 Preset-Tasten an SoundTouch-Lautsprechern zurück, ganz ohne Cloud.
</p>

<p align="center"><a href="#english">English</a> · <a href="#deutsch">Deutsch</a></p>

---

## English

Since the SoundTouch cloud was switched off in May 2026, the preset buttons 1–6 on the speakers no longer work. Boom Box Bridge fixes that. It runs as a small Docker container on any always-on device in your home network (Synology, QNAP, Raspberry Pi, home server).

It works on its own with a built-in web page. Together with the **Boom Box for SoundTouch** iPhone app, the 6 presets in the app become the 6 buttons on your speaker.

### How it works

```
Button on speaker ──► WebSocket event (speaker, port 8080)
                          │
                          ▼
                   Boom Box Bridge  ── looks up the station for that button
                          │
                          ▼
              UPnP SetAVTransportURI + Play (speaker, port 8091)
                          │
                          ▼
           The speaker streams the station directly
```

- No firmware changes, no rooting, nothing is installed on the speaker
- Finds speakers automatically (Bonjour), or by fixed IP address
- After power off/on the speaker shows "INVALID_SOURCE"; the bridge resumes the last station per speaker
- Web page with station search (radio-browser.info) and your own stream URLs
- No account, no tracking, no data leaves your network (apart from the radio streams themselves)

Tested with SoundTouch 10 and 20. Other SoundTouch models with the same local API should work.

### Install on a Synology (Container Manager)

Requires DSM 7.2 or newer with the **Container Manager** package.

1. In **File Station**, create the folder `docker/boombox-bridge` and inside it an empty folder `data`.
2. Upload [`docker-compose.yml`](docker-compose.yml) into `docker/boombox-bridge`.
3. **Container Manager → Project → Create**
   - Name: `boombox-bridge`, Path: `docker/boombox-bridge`
   - Source: **Use existing docker-compose.yml**
   - Skip the Web Station step → **Done**
4. Open `http://<your-NAS-IP>:8765` in a browser. Your speakers appear after about a minute.
5. Click **Assign** on a button, pick a station, done. Press the button on your speaker.

### Install with Docker Compose (Raspberry Pi, Linux)

```bash
mkdir boombox-bridge && cd boombox-bridge
curl -O https://raw.githubusercontent.com/kern-labs/boombox-bridge/main/docker-compose.yml
mkdir data
docker compose up -d
```

Then open `http://<device-IP>:8765`.

### Settings (docker-compose.yml)

| Variable | Default | Meaning |
|---|---|---|
| `BRIDGE_PORT` | `8765` | Port of the web page and API |
| `SPEAKERS` | empty | Fixed IP addresses, comma-separated, if discovery finds nothing |
| `AUTO_RESUME` | `on` | Resume the last station after power off/on |
| `DISCOVERY` | `on` | Bonjour discovery of speakers |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every event from the speakers |

`network_mode: host` is required so the bridge can find the speakers via Bonjour.

### Troubleshooting

- **No speakers found:** add them by IP on the web page, or set `SPEAKERS=`. Give your speakers fixed IP addresses in the router.
- **Page not reachable:** if the NAS firewall is on, allow TCP port 8765 from your home network.
- **Button pressed, nothing happens:** set `LOG_LEVEL=DEBUG`, restart, press the button and check the container log.

### Security

The bridge has no login and is meant for your home network only. **Do not** forward port 8765 to the internet.

### API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | Version and connection status |
| GET | `/api/speakers` | Speakers with their 6 presets |
| POST | `/api/speakers` | `{"host":"192.168.1.50"}` add a speaker |
| GET/PUT | `/api/presets/{deviceId}` | Read/write presets (`*` = default for all) |
| POST | `/api/play/{deviceId}` | `{"slot":1}` or `{"name":"…","url":"…"}` play now |

---

## Deutsch

Seit die SoundTouch-Cloud im Mai 2026 abgeschaltet wurde, funktionieren die Preset-Tasten 1–6 an den Lautsprechern nicht mehr. Die Boom Box Bridge löst das. Sie läuft als kleiner Docker-Container auf einem Gerät, das immer an ist (Synology, QNAP, Raspberry Pi, Heimserver).

Sie funktioniert eigenständig mit eigener Webseite. Zusammen mit der iPhone-App **Boom Box for SoundTouch** sind die 6 Presets der App gleichzeitig die 6 Tasten am Lautsprecher.

### Was sie kann

- Keine Änderung an der Firmware, nichts wird auf dem Lautsprecher installiert
- Findet Lautsprecher automatisch (Bonjour) oder über feste IP-Adressen
- Nach Aus/Ein zeigt der Lautsprecher "INVALID_SOURCE"; die Bridge spielt den letzten Sender pro Lautsprecher weiter
- Webseite mit Sendersuche (radio-browser.info) und eigenen Stream-Adressen
- Kein Konto, kein Tracking, keine Daten verlassen dein Netz (ausser den Radio-Streams selbst)

Getestet mit SoundTouch 10 und 20.

### Installation auf der Synology (Container Manager)

Voraussetzung: DSM 7.2 oder neuer mit dem Paket **Container Manager**.

1. In der **File Station** den Ordner `docker/boombox-bridge` anlegen, darin einen leeren Ordner `data`.
2. Die Datei [`docker-compose.yml`](docker-compose.yml) nach `docker/boombox-bridge` hochladen.
3. **Container Manager → Projekt → Erstellen**
   - Name: `boombox-bridge`, Pfad: `docker/boombox-bridge`
   - Quelle: **Vorhandene docker-compose.yml verwenden**
   - Web-Station-Schritt überspringen → **Fertig**
4. Im Browser `http://IP-DEINER-SYNOLOGY:8765` öffnen. Nach etwa einer Minute erscheinen deine Lautsprecher.
5. Bei einer Taste auf **Belegen** klicken, Sender wählen, fertig. Taste am Lautsprecher drücken.

### Installation mit Docker Compose (Raspberry Pi, Linux)

```bash
mkdir boombox-bridge && cd boombox-bridge
curl -O https://raw.githubusercontent.com/kern-labs/boombox-bridge/main/docker-compose.yml
mkdir data
docker compose up -d
```

Danach `http://IP-DES-GERÄTS:8765` öffnen.

### Einstellungen und Fehlersuche

Die Einstellungen stehen oben in der englischen Tabelle und als Kommentare in der `docker-compose.yml`.

- **Keine Lautsprecher gefunden:** auf der Webseite per IP hinzufügen oder `SPEAKERS=` setzen. Im Router feste IP-Adressen für die Lautsprecher vergeben.
- **Seite nicht erreichbar:** Ist die Firewall der Synology aktiv, TCP-Port 8765 aus dem Heimnetz erlauben.
- **Taste gedrückt, nichts passiert:** `LOG_LEVEL=DEBUG` setzen, neu starten, Taste drücken, Protokoll des Containers ansehen.

**Sicherheit:** Die Bridge hat keine Anmeldung und ist nur fürs Heimnetz gedacht. Port 8765 **nicht** im Router nach aussen freigeben.

---

## Credits / Danke

B## Credits / Danke

Boom Box Bridge contains no code from other projects, but it builds on what these
projects found out after the SoundTouch cloud shutdown in 2026. Thank you!
Die Bridge enthält keinen fremden Code, baut aber auf den Erkenntnissen dieser
Projekte auf. Danke!

- [sandervg/homeassistant-bose-soundtouch-bridge](https://github.com/sandervg/homeassistant-bose-soundtouch-bridge)
  – preset buttons via the speaker's local WebSocket and UPnP playback
- [nsabba/bose-preset-bridge](https://github.com/nsabba/bose-preset-bridge)
  – the `nowSelectionUpdated` event and the AVTransport control URL
---

## License / Lizenz

MIT, see [LICENSE](LICENSE). © 2026 Tobias Kern (Kernlabs)

Boom Box Bridge is an independent project and is not affiliated with, endorsed or sponsored by Bose Corporation. Bose and SoundTouch are trademarks of Bose Corporation.
