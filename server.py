"""
CS2 Context-Aware Grenade Lineup Assistant - proof of concept.

Receives Counter-Strike 2 Game State Integration (GSI) data over HTTP,
works out the current map, side and round phase for the local player,
and pushes matching lineups to a browser page in real time.

Standard library only: run with `python server.py` (Python 3.9+).

Endpoints
  POST /        GSI receiver (CS2 posts here)
  GET  /        Web UI
  GET  /events  Server-Sent Events stream of the current context + lineups
  GET  /state   Current context as JSON (for debugging)
"""

import argparse
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
WEB_DIR = ROOT / "web"
LINEUP_DIR = ROOT / "lineups"
MEDIA_DIR = ROOT / "media"

AUTH_TOKEN = "nadehelper-poc-token"  # must match the .cfg file

# --------------------------------------------------------------------------
# Lineup database
# --------------------------------------------------------------------------

def load_lineups():
    """Load every lineups/<map>.json file into {map_name: [lineup, ...]}."""
    db = {}
    for path in sorted(LINEUP_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        db[data["map"]] = data["lineups"]
    return db


LINEUPS = load_lineups()

# Grenade names as GSI reports them -> our short type names
WEAPON_TO_NADE = {
    "weapon_smokegrenade": "smoke",
    "weapon_flashbang": "flash",
    "weapon_molotov": "molotov",
    "weapon_incgrenade": "molotov",
    "weapon_hegrenade": "he",
    "weapon_decoy": "decoy",
}

# --------------------------------------------------------------------------
# Game context extracted from GSI
# --------------------------------------------------------------------------

class Context:
    def __init__(self):
        self.lock = threading.Lock()
        self.map = None
        self.side = None          # "T" or "CT"
        self.mode = None          # competitive, premier, casual, ...
        self.map_phase = None     # warmup, live, intermission, gameover
        self.round_phase = None   # freezetime, live, over
        self.round = None
        self.nades = []           # grenades the local player is carrying
        self.is_local = None      # is the data about the local player?
        self.last_update = None
        self.updates = 0

    def snapshot(self):
        with self.lock:
            return {
                "map": self.map,
                "side": self.side,
                "mode": self.mode,
                "map_phase": self.map_phase,
                "round_phase": self.round_phase,
                "round": self.round,
                "nades": self.nades,
                "is_local": self.is_local,
                "last_update": self.last_update,
                "updates": self.updates,
                "map_supported": self.map in LINEUPS,
            }

    def update(self, payload):
        """Apply one GSI payload. Returns True if anything relevant changed."""
        before = self.snapshot()
        with self.lock:
            game_map = payload.get("map") or {}
            player = payload.get("player") or {}
            provider = payload.get("provider") or {}
            rnd = payload.get("round") or {}

            if not game_map:  # in the main menu: no map block at all
                self.map = self.side = self.mode = None
                self.map_phase = self.round_phase = self.round = None
                self.nades = []
                self.is_local = None
            else:
                self.map = game_map.get("name", self.map)
                self.mode = game_map.get("mode", self.mode)
                self.map_phase = game_map.get("phase", self.map_phase)
                self.round = game_map.get("round", self.round)
                self.round_phase = rnd.get("phase", self.round_phase)

                # The player block describes whoever we are watching. When
                # dead and spectating a teammate it is *their* data, so only
                # trust player-specific fields when the steamids match.
                local_id = provider.get("steamid")
                player_id = player.get("steamid")
                self.is_local = bool(local_id and player_id == local_id)

                if self.is_local:
                    team = player.get("team")
                    if team in ("T", "CT"):
                        self.side = team
                    self.nades = carried_nades(player.get("weapons") or {})
                elif player.get("team") in ("T", "CT") and self.side is None:
                    # Spectating a teammate before we ever saw our own data:
                    # the side is still the same as ours.
                    self.side = player["team"]

            self.last_update = time.time()
            self.updates += 1
        after = self.snapshot()
        keys = ("map", "side", "mode", "map_phase", "round_phase", "round",
                "nades", "is_local")
        return any(before[k] != after[k] for k in keys)


def carried_nades(weapons):
    nades = []
    for w in weapons.values():
        nade = WEAPON_TO_NADE.get(w.get("name"))
        if nade and nade not in nades:
            nades.append(nade)
    return nades


CONTEXT = Context()

# --------------------------------------------------------------------------
# Recommendation logic
# --------------------------------------------------------------------------

def recommend(ctx):
    """Return the lineups that fit the current context, best first."""
    lineups = LINEUPS.get(ctx["map"], [])
    side = ctx["side"]
    result = [l for l in lineups if side is None or l["side"] == side]

    carried = set(ctx["nades"])

    def score(l):
        s = 0
        if l["type"] in carried:
            s += 10           # grenades you actually have come first
        s += l.get("priority", 0)
        return -s

    result.sort(key=score)
    return [dict(l, carried=l["type"] in carried) for l in result]


def build_message():
    ctx = CONTEXT.snapshot()
    return {"context": ctx, "lineups": recommend(ctx)}

# --------------------------------------------------------------------------
# Server-Sent Events: push updates to every open browser tab
# --------------------------------------------------------------------------

SUBSCRIBERS = set()
SUB_LOCK = threading.Lock()


def broadcast():
    data = json.dumps(build_message())
    with SUB_LOCK:
        for q in list(SUBSCRIBERS):
            q.put(data)

# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript",
    ".css": "text/css",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    log_file = None
    quiet = True

    def log_message(self, fmt, *args):
        if not self.quiet:
            super().log_message(fmt, *args)

    # ---- GSI receiver ----------------------------------------------------
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return

        token = (payload.get("auth") or {}).get("token")
        if token != AUTH_TOKEN:
            print("Rejected payload with wrong auth token")
            return

        received = time.time()
        if CONTEXT.updates == 0:
            in_game = "map" in payload
            print(f"[{time.strftime('%H:%M:%S')}] First data received from CS2 "
                  f"({'in a match' if in_game else 'main menu'})")
        if self.log_file:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"received": received, "payload": payload}) + "\n")

        if CONTEXT.update(payload):
            c = CONTEXT.snapshot()
            print(f"[{time.strftime('%H:%M:%S')}] map={c['map']} side={c['side']} "
                  f"phase={c['round_phase']} nades={c['nades']} local={c['is_local']}")
            broadcast()

    # ---- UI, state and live stream ---------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/events":
            return self.stream_events()
        if path == "/state":
            return self.send_json(build_message())
        if path == "/":
            path = "/index.html"
        if path.startswith("/media/"):
            return self.send_static(MEDIA_DIR, path[len("/media/"):])
        return self.send_static(WEB_DIR, path.lstrip("/"))

    def send_json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_static(self, base, rel):
        target = (base / rel).resolve()
        if base.resolve() not in target.parents or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q = queue.Queue()
        with SUB_LOCK:
            SUBSCRIBERS.add(q)
        try:
            q.put(json.dumps(build_message()))  # send current state immediately
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(f"data: {data}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with SUB_LOCK:
                SUBSCRIBERS.discard(q)


def main():
    parser = argparse.ArgumentParser(description="CS2 grenade lineup assistant (PoC)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Use 0.0.0.0 to open the UI on a phone in the same network")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--log", metavar="FILE",
                        help="Append every raw GSI payload to FILE (JSON lines) for analysis")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    Handler.log_file = args.log
    Handler.quiet = not args.verbose

    # On Windows, the default address reuse lets a second copy of the server
    # bind the same port silently, and CS2 may then post to the wrong one.
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError:
        print(f"Port {args.port} is already in use. Close the other copy of the "
              f"server (or the other GSI app using it) and try again.")
        return
    server.daemon_threads = True
    maps = ", ".join(f"{m} ({len(l)})" for m, l in LINEUPS.items())
    print(f"Lineups loaded: {maps}")
    print(f"Listening for CS2 on http://{args.host}:{args.port}")
    print(f"Open the UI at   http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
