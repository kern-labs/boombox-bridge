"""Simulierter SoundTouch-Lautsprecher für Tests (Web-API, WebSocket gabbo, UPnP)."""
import asyncio, sys, json
from aiohttp import web, WSMsgType

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.2"
LOG = sys.argv[2] if len(sys.argv) > 2 else "/tmp/fake_soap.log"
clients = set()

async def info(_):
    return web.Response(text='<?xml version="1.0" encoding="UTF-8" ?><info deviceID="AABBCCDDEEFF"><name>Kueche</name><type>SoundTouch 20</type></info>', content_type="text/xml")

async def ws_handler(request):
    ws = web.WebSocketResponse(protocols=("gabbo",))
    await ws.prepare(request)
    clients.add(ws)
    async for _ in ws: pass
    clients.discard(ws)
    return ws

async def press(request):
    slot = request.query.get("slot", "1")
    msg = f'<updates deviceID="AABBCCDDEEFF"><nowSelectionUpdated><preset id="{slot}"><ContentItem source="INVALID_SOURCE" isPresetable="true"><itemName>x</itemName></ContentItem></preset></nowSelectionUpdated></updates>'
    for ws in list(clients):
        await ws.send_str(msg)
    # zusätzlich ein irrelevantes Ereignis
    for ws in list(clients):
        await ws.send_str('<updates deviceID="AABBCCDDEEFF"><volumeUpdated><volume><actualvolume>20</actualvolume></volume></volumeUpdated></updates>')
    return web.Response(text=f"sent to {len(clients)}")

async def np_event(request):
    src = request.query.get("source", "INVALID_SOURCE")
    loc = request.query.get("location", "")
    name = request.query.get("name", "")
    item = f'<ContentItem source="{src}" location="{loc}"><itemName>{name}</itemName></ContentItem>' if loc else ''
    msg = f'<updates deviceID="AABBCCDDEEFF"><nowPlayingUpdated><nowPlaying deviceID="AABBCCDDEEFF" source="{src}">{item}</nowPlaying></nowPlayingUpdated></updates>'
    for ws in list(clients):
        await ws.send_str(msg)
    return web.Response(text="ok")

STATE = {"fail_next_play": False}

async def arm(request):
    STATE["fail_next_play"] = True
    return web.Response(text="armed")

async def soap(request):
    body = await request.text()
    action = request.headers.get("SOAPACTION", "")
    if action.endswith('#Play"') and STATE["fail_next_play"]:
        STATE["fail_next_play"] = False
        with open(LOG, "a") as f:
            f.write(json.dumps({"action": action, "body": "FAULT"}) + "\n")
        return web.Response(status=500, text='<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault><detail><UPnPError><errorCode>716</errorCode><errorDescription>No URI supplied</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>', content_type="text/xml")
    with open(LOG, "a") as f:
        f.write(json.dumps({"action": request.headers.get("SOAPACTION"), "body": body}) + "\n")
    return web.Response(text="<ok/>", content_type="text/xml")

async def main():
    api = web.Application(); api.router.add_get("/info", info); api.router.add_get("/press", press); api.router.add_get("/np", np_event); api.router.add_get("/arm", arm)
    wsapp = web.Application(); wsapp.router.add_get("/", ws_handler)
    upnp = web.Application(); upnp.router.add_post("/AVTransport/Control", soap)
    for app, port in ((api, 18090), (wsapp, 18080), (upnp, 18091)):
        r = web.AppRunner(app); await r.setup(); await web.TCPSite(r, HOST, port).start()
    print("fake speaker ready", flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
