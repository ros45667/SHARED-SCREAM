import asyncio
import ipaddress
import json
import math
import os
import random
import socket
import sys
from datetime import datetime, timedelta, timezone
from typing import List

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# ==========================================
# WINDOWS + SSL WORKAROUND
#
# asyncio's default event loop on Windows (ProactorEventLoop) has a known
# bug: whenever an SSL/TLS connection is closed abruptly -- a browser tab
# closing, a phone locking its screen, a flaky WiFi hop, or literally any
# ordinary disconnect, not just a rejected certificate -- it logs a scary
# "Exception in callback ... ConnectionResetError [WinError 10054]"
# traceback to the console. It's cosmetic noise, not a real error, but it
# drowns out logs that might actually matter. SelectorEventLoop doesn't have
# this bug, so we switch to it on Windows before anything else starts.
# ==========================================
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ==========================================
# OPTIONAL SPEED-UPS (used automatically if installed, never required)
#
# uvloop swaps in a much faster event loop (Linux/macOS only).
# orjson serializes the game state noticeably faster than stdlib json.
# Both fall back silently if not installed, so low-end machines without
# them still work -- they just get a smaller speed boost.
# ==========================================
try:
    import uvloop
    uvloop.install()
except ImportError:
    uvloop = None

try:
    import orjson

    def dumps(obj) -> str:
        return orjson.dumps(obj).decode("utf-8")
except ImportError:
    def dumps(obj) -> str:
        # separators=(",", ":") strips the default spaces -> smaller payload,
        # less to serialize and less to push over (possibly weak) Wi-Fi.
        return json.dumps(obj, separators=(",", ":"))


# ==========================================
# PERFORMANCE TUNING (override via environment variables, no code edits needed)
#
# On a low-end machine, try:  BROADCAST_HZ=20 python main.py
# That keeps physics accurate at 60Hz but only sends network updates
# 20x/sec, which is usually the actual bottleneck on weak CPUs/Wi-Fi --
# not the physics math itself.
# ==========================================
TICK_HZ = int(os.environ.get("TICK_HZ", "60"))
BROADCAST_HZ = int(os.environ.get("BROADCAST_HZ", str(TICK_HZ)))
TICK_DT = 1 / TICK_HZ
TICKS_PER_BROADCAST = max(1, round(TICK_HZ / BROADCAST_HZ))

# ==========================================
# HTTP + HTTPS, SIDE BY SIDE
#
# Laptops (the two displays) are served over plain HTTP on HTTP_PORT --
# no cert warning to click through, no self-signed-cert friction.
#
# Phones (the controllers) need HTTPS on HTTPS_PORT, because:
#   1. DeviceOrientation/DeviceMotion (used by controller.html's motion
#      mode) are only exposed by phone browsers on a secure context.
#   2. A page served over https:// is not allowed to open a plain ws://
#      socket (mixed content) -- controller.html picks ws/wss based on
#      the protocol it was loaded with, so it needs to be https.
#
# Both listeners run in the SAME process against the SAME FastAPI app,
# so there's exactly one game_loop, one ConnectionManager, and one
# game_state shared by every device no matter which port/protocol it
# connected through.
# ==========================================
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8000"))
HTTPS_PORT = int(os.environ.get("HTTPS_PORT", "8443"))

# ==========================================
# COURT CONFIGURATION
#
# The "court" is one continuous playing field that is TWICE as wide as a
# single screen. Screen "left" renders world-x [0, CANVAS_WIDTH), screen
# "right" renders world-x [CANVAS_WIDTH, COURT_WIDTH). The ball's position
# is tracked once, here, in world coordinates -- each display just clips
# to its own half, which is what makes the ball appear to travel from one
# laptop's screen to the other's.
# ==========================================
CANVAS_WIDTH = 800          # width of ONE screen/court-half
CANVAS_HEIGHT = 600
COURT_WIDTH = CANVAS_WIDTH * 2

BALL_SIZE = 15
PADDLE_WIDTH = 15
PADDLE_HEIGHT = 100         # BASE height -- screaming temporarily grows a paddle above this
PADDLE_SPEED = 8
PADDLE_MARGIN = 30          # distance of each paddle from its outer wall

PADDLE1_X = PADDLE_MARGIN
PADDLE2_X = COURT_WIDTH - PADDLE_MARGIN - PADDLE_WIDTH

# ==========================================
# SCREAM MECHANIC
#
# The phone controller listens to the mic and reports a loudness "level"
# (0.0 quiet -> 1.0 screaming) several times a second. Each paddle tracks
# its own scream "charge" in game_state: incoming levels can only push the
# charge UP instantly (so a scream registers right away), while step_physics
# decays it back down every tick when the player isn't screaming -- like a
# peak-hold VU meter. Paddle height is derived from that charge each tick,
# maxing out at PADDLE_SCREAM_MAX_BONUS above the base height.
# ==========================================
PADDLE_SCREAM_MAX_BONUS = 0.5     # +50% height at full charge -> 1.5x base, as requested
SCREAM_DECAY_PER_TICK = 0.985     # charge *= this every tick; ~2-3s to fade back to normal

# ==========================================
# POWERUPS
#
# A random powerup spawns once every POWERUP_MIN_HITS-POWERUP_MAX_HITS
# paddle hits (re-rolled fresh after each spawn), sitting in the middle of
# the court until the BALL touches it. It then speeds the ball up or slows
# it down and disappears. Speed is clamped so repeated powerups can't spiral
# the ball into being unhittable (too fast) or boring (too slow).
#
# Lowered from 10-20 to 3-6 so a powerup shows up every couple of rallies
# instead of only in longer ones -- makes them a regular part of play
# instead of a rare event most short games never even see.
# ==========================================
POWERUP_MIN_HITS = 3
POWERUP_MAX_HITS = 6
POWERUP_SIZE = 24
POWERUP_SPEED_MULT = {"fast": 1.35, "slow": 0.72}
BALL_MIN_SPEED = 4.0
BALL_MAX_SPEED = 16.0

game_state = {
    "court": {"width": COURT_WIDTH, "height": CANVAS_HEIGHT, "screen_width": CANVAS_WIDTH},
    "ball": {"x": COURT_WIDTH / 2, "y": CANVAS_HEIGHT / 2, "size": BALL_SIZE, "vx": 6, "vy": 5},
    "paddles": {
        "p1": {"x": PADDLE1_X, "y": CANVAS_HEIGHT / 2 - PADDLE_HEIGHT / 2, "height": PADDLE_HEIGHT, "scream": 0.0},
        "p2": {"x": PADDLE2_X, "y": CANVAS_HEIGHT / 2 - PADDLE_HEIGHT / 2, "height": PADDLE_HEIGHT, "scream": 0.0},
    },
    "paddle_size": {"width": PADDLE_WIDTH},
    "score": {"p1": 0, "p2": 0},
    "powerup": None,  # None, or {"x", "y", "size", "type": "fast" | "slow"}
}

# Paddle-hit counter driving powerup spawns -- re-rolled every time a
# powerup is picked up (or spawned). Not part of game_state since the client
# never needs to know the raw count, only whether a powerup is on the court.
_hits_since_powerup = 0
_next_powerup_at = random.randint(POWERUP_MIN_HITS, POWERUP_MAX_HITS)

# Reused every broadcast instead of allocating a fresh dict each tick.
# Coordinates are rounded to whole pixels for the wire -- the client only
# ever draws integer pixels anyway, and shorter numbers serialize faster
# and take less bandwidth than raw floats like 505.32999999999993.
_wire = {
    "court": game_state["court"],
    "ball": {"x": 0, "y": 0, "size": BALL_SIZE},
    "paddles": {
        "p1": {"x": PADDLE1_X, "y": 0, "height": PADDLE_HEIGHT},
        "p2": {"x": PADDLE2_X, "y": 0, "height": PADDLE_HEIGHT},
    },
    "paddle_size": game_state["paddle_size"],
    "score": game_state["score"],
    "powerup": None,
}


def get_lan_ip() -> str:
    """Best-effort guess at this machine's LAN IP (no packets actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_all_local_ips() -> List[str]:
    """
    Every plausible LAN-facing IPv4 address this machine currently has, not
    just the single one get_lan_ip() guesses. On a laptop with more than one
    active adapter (WiFi + Ethernet, a VPN, a Docker/VMware/Hyper-V virtual
    adapter, etc), that one guess can resolve to the wrong adapter -- which
    produces a TLS cert that doesn't cover the address other devices are
    actually using to reach this machine. A plain HTTPS page load usually
    still lets you click through that mismatch, but the WebSocket connection
    the page then opens often does not get the same pass, which shows up as
    "loads fine, then immediately disconnects and keeps retrying" instead of
    an obvious certificate error. Covering every address we can find sidesteps
    the guessing problem entirely.
    """
    ips = set()

    try:
        ips.add(get_lan_ip())
    except Exception:
        pass

    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        ips.update(addrs)
    except Exception:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass

    ips.discard("127.0.0.1")
    return sorted(ips)


CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"


def ensure_self_signed_cert(cert_path: str, key_path: str, lan_ips: List[str]):
    """
    Make sure a TLS cert/key pair exists so we can serve over HTTPS, generating
    a throwaway self-signed one if needed. HTTPS is required here for two
    reasons: phones only expose DeviceOrientation/DeviceMotion (used by the
    controller's motion mode) on a secure context, and a page served over
    https:// is not allowed to open a plain ws:// socket (mixed content).

    Regenerates the cert if it's missing, expired, or doesn't cover every IP
    this machine currently has (e.g. it joined a different network since the
    last run, or picked up an address on a new adapter).
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    if os.path.exists(cert_path) and os.path.exists(key_path):
        try:
            with open(cert_path, "rb") as f:
                existing = x509.load_pem_x509_certificate(f.read())
            san = existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            covered_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
            if set(lan_ips) <= covered_ips and existing.not_valid_after_utc > datetime.now(timezone.utc):
                return  # existing cert is still good
        except Exception:
            pass  # anything wrong with the existing file -> just regenerate

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Scream Pong (dev, self-signed)")])

    san_entries = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    for ip in lan_ips:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    covered = ["localhost", "127.0.0.1"] + lan_ips
    print(f"[+] Generated self-signed TLS cert covering: {', '.join(covered)} (valid 365 days) -> {cert_path}")


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"[+] Client connected. Total active clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            print(f"[-] Client disconnected. Total active clients: {len(self.active_connections)}")

    async def _send_one(self, connection: WebSocket, message: str):
        try:
            # A slow/stalled client shouldn't be able to hold up everyone
            # else -- give it a short timeout and drop it if it can't keep up.
            await asyncio.wait_for(connection.send_text(message), timeout=0.5)
        except Exception:
            self.disconnect(connection)

    def broadcast_nowait(self, message: str):
        """
        Fire-and-forget broadcast. This is the key fix for lag: the old
        version used `await asyncio.gather(...)`, which meant the physics
        loop had to wait for EVERY connected client's send to finish before
        it could step forward again -- so one laggy device stalled the
        game for everyone. Scheduling each send as its own task lets the
        physics loop keep ticking at full speed regardless of network
        conditions on any individual client.
        """
        if not self.active_connections:
            return
        for connection in list(self.active_connections):
            asyncio.create_task(self._send_one(connection, message))


manager = ConnectionManager()


def reset_ball(serve_towards: int = 1):
    """serve_towards: 1 -> ball moves toward player 1 (negative vx), 2 -> toward player 2."""
    game_state["ball"]["x"] = COURT_WIDTH / 2
    game_state["ball"]["y"] = CANVAS_HEIGHT / 2
    game_state["ball"]["vx"] = -6 if serve_towards == 1 else 6
    game_state["ball"]["vy"] = 5 if game_state["ball"]["vy"] >= 0 else -5
    game_state["powerup"] = None


def _spawn_powerup():
    """Drop a new powerup somewhere in the middle third of the court, away
    from either paddle, with a 50/50 fast-or-slow effect."""
    margin = COURT_WIDTH * 0.15  # keep it out of the paddles' immediate reach
    x = random.uniform(PADDLE1_X + margin, PADDLE2_X - margin)
    y = random.uniform(0, CANVAS_HEIGHT - POWERUP_SIZE)
    kind = random.choice(["fast", "slow"])
    game_state["powerup"] = {"x": x, "y": y, "size": POWERUP_SIZE, "type": kind}


def _register_paddle_hit():
    """Call once per successful paddle bounce. Spawns a powerup once the
    random 10-20 hit threshold is reached (only if none is already out)."""
    global _hits_since_powerup, _next_powerup_at
    _hits_since_powerup += 1
    if game_state["powerup"] is None and _hits_since_powerup >= _next_powerup_at:
        _spawn_powerup()
        _hits_since_powerup = 0
        _next_powerup_at = random.randint(POWERUP_MIN_HITS, POWERUP_MAX_HITS)


def step_physics():
    ball = game_state["ball"]
    p1 = game_state["paddles"]["p1"]
    p2 = game_state["paddles"]["p2"]

    # ---- Scream charge decay -> paddle height ----
    # Charge only ever gets pushed UP by an incoming "scream" websocket
    # message (see websocket_endpoint); here it just fades back down, so a
    # paddle grows the instant you scream and shrinks back over a couple
    # seconds of quiet. Height is derived fresh from charge every tick.
    for paddle in (p1, p2):
        paddle["scream"] *= SCREAM_DECAY_PER_TICK
        if paddle["scream"] < 0.001:
            paddle["scream"] = 0.0
        paddle["height"] = PADDLE_HEIGHT * (1 + PADDLE_SCREAM_MAX_BONUS * paddle["scream"])
        # A paddle that grew while already near the bottom edge shouldn't
        # be allowed to poke out past the court boundary.
        paddle["y"] = min(paddle["y"], CANVAS_HEIGHT - paddle["height"])

    # Move ball
    ball["x"] += ball["vx"]
    ball["y"] += ball["vy"]

    # Bounce off top & bottom of the court
    if ball["y"] <= 0:
        ball["y"] = 0
        ball["vy"] *= -1
    elif ball["y"] >= CANVAS_HEIGHT - BALL_SIZE:
        ball["y"] = CANVAS_HEIGHT - BALL_SIZE
        ball["vy"] *= -1

    # Paddle 1 collision (left wall of the court)
    if (
        ball["vx"] < 0
        and ball["x"] <= p1["x"] + PADDLE_WIDTH
        and ball["x"] + BALL_SIZE >= p1["x"]
        and ball["y"] + BALL_SIZE >= p1["y"]
        and ball["y"] <= p1["y"] + p1["height"]
    ):
        ball["x"] = p1["x"] + PADDLE_WIDTH
        ball["vx"] *= -1
        _register_paddle_hit()

    # Paddle 2 collision (right wall of the court)
    if (
        ball["vx"] > 0
        and ball["x"] + BALL_SIZE >= p2["x"]
        and ball["x"] <= p2["x"] + PADDLE_WIDTH
        and ball["y"] + BALL_SIZE >= p2["y"]
        and ball["y"] <= p2["y"] + p2["height"]
    ):
        ball["x"] = p2["x"] - BALL_SIZE
        ball["vx"] *= -1
        _register_paddle_hit()

    # ---- Powerup pickup: ball touches it -> speeds it up or slows it down ----
    powerup = game_state["powerup"]
    if powerup is not None:
        closest_x = max(powerup["x"], min(ball["x"] + BALL_SIZE / 2, powerup["x"] + powerup["size"]))
        closest_y = max(powerup["y"], min(ball["y"] + BALL_SIZE / 2, powerup["y"] + powerup["size"]))
        dx = (ball["x"] + BALL_SIZE / 2) - closest_x
        dy = (ball["y"] + BALL_SIZE / 2) - closest_y
        if dx * dx + dy * dy <= (BALL_SIZE / 2) ** 2:
            mult = POWERUP_SPEED_MULT[powerup["type"]]
            speed = math.hypot(ball["vx"], ball["vy"])
            new_speed = max(BALL_MIN_SPEED, min(BALL_MAX_SPEED, speed * mult))
            scale = new_speed / speed if speed else 1.0
            ball["vx"] *= scale
            ball["vy"] *= scale
            game_state["powerup"] = None

    # Scoring -- ball passed a paddle and left the court entirely
    if ball["x"] < 0:
        game_state["score"]["p2"] += 1
        reset_ball(serve_towards=1)
    elif ball["x"] > COURT_WIDTH:
        game_state["score"]["p1"] += 1
        reset_ball(serve_towards=2)


def build_wire_message() -> str:
    """Round to whole pixels into the reused _wire dict, then serialize once."""
    ball = game_state["ball"]
    p1 = game_state["paddles"]["p1"]
    p2 = game_state["paddles"]["p2"]
    powerup = game_state["powerup"]

    wb = _wire["ball"]
    wb["x"] = int(ball["x"])
    wb["y"] = int(ball["y"])

    _wire["paddles"]["p1"]["y"] = int(p1["y"])
    _wire["paddles"]["p1"]["height"] = int(p1["height"])
    _wire["paddles"]["p2"]["y"] = int(p2["y"])
    _wire["paddles"]["p2"]["height"] = int(p2["height"])

    if powerup is None:
        _wire["powerup"] = None
    else:
        _wire["powerup"] = {
            "x": int(powerup["x"]),
            "y": int(powerup["y"]),
            "size": powerup["size"],
            "type": powerup["type"],
        }

    return dumps(_wire)


async def game_loop():
    """
    Authoritative physics loop, ticking at TICK_HZ. This is the ONLY place
    game state changes. Broadcasting is decoupled (see TICKS_PER_BROADCAST)
    so you can keep smooth 60Hz physics while sending fewer, cheaper
    updates over the network on constrained hardware.
    """
    loop = asyncio.get_event_loop()
    next_tick = loop.time()
    tick_count = 0

    while True:
        step_physics()
        tick_count += 1

        if tick_count % TICKS_PER_BROADCAST == 0:
            manager.broadcast_nowait(build_wire_message())

        # Schedule the next tick relative to a fixed clock instead of just
        # sleeping TICK_DT after finishing work. This prevents drift from
        # accumulating on a slow CPU (each tick creeping later than the
        # last) instead of just running a bit slow but steadily.
        next_tick += TICK_DT
        delay = next_tick - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            # We've fallen behind -- don't try to "catch up" with a burst
            # of instant ticks (that would spike CPU further on hardware
            # that's already struggling). Just resync to now and continue.
            next_tick = loop.time()
            await asyncio.sleep(0)  # yield control back to the event loop


app = FastAPI()


@app.get("/")
async def get_main_display():
    with open("index.html", "r", encoding="utf-8") as file:
        html_content = file.read()
    # index.html's QR code needs to know the HTTPS port so it can build a
    # secure URL for phones even though this page is normally loaded over
    # plain HTTP -- see the __HTTPS_PORT__ placeholder near its <head>.
    html_content = html_content.replace("__HTTPS_PORT__", str(HTTPS_PORT))
    return HTMLResponse(content=html_content)


@app.get("/controller")
async def get_controller():
    with open("controller.html", "r", encoding="utf-8") as file:
        html_content = file.read()
    return HTMLResponse(content=html_content)


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """
    A single generic endpoint for every device (display or controller).
    client_id is only used for logging -- any connection may send paddle
    commands, and the payload itself says which player it's driving:
        {"action": "up" | "down", "player": 1 | 2}
        {"action": "set", "value": 0.0-1.0, "player": 1 | 2}
        {"action": "scream", "level": 0.0-1.0, "player": 1 | 2}
    "up"/"down" nudge the paddle by a fixed step each message -- used by
    the keyboard and the button controller, where holding the key/button
    resends the same action repeatedly.
    "set" jumps the paddle straight to an absolute position (0.0 = top of
    its track, 1.0 = bottom) -- used by the phone's tilt controls, where
    the tilt angle itself already represents "how far up/down", not a
    direction to nudge in.
    "scream" reports the controller's current mic loudness; it can only
    push that paddle's scream charge UP immediately (step_physics decays it
    back down every tick), which is what grows the paddle while you're
    screaming and shrinks it back once you stop.
    This way a laptop's own keyboard AND an optional phone controller can
    both drive the same paddle without the server needing to special-case
    where the message came from.
    """
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue

            action = msg.get("action")
            player = msg.get("player")
            if action not in ("up", "down", "set", "scream") or player not in (1, 2):
                continue

            paddle = game_state["paddles"]["p1"] if player == 1 else game_state["paddles"]["p2"]
            # Height can currently be inflated by screaming, so the movement
            # clamp has to use the paddle's CURRENT height, not the base one.
            max_y = CANVAS_HEIGHT - paddle["height"]

            if action == "up":
                paddle["y"] = max(0, paddle["y"] - PADDLE_SPEED)
            elif action == "down":
                paddle["y"] = min(max_y, paddle["y"] + PADDLE_SPEED)
            elif action == "set":
                value = msg.get("value")
                if not isinstance(value, (int, float)):
                    continue
                paddle["y"] = max(0.0, min(1.0, value)) * max_y
            elif action == "scream":
                level = msg.get("level")
                if not isinstance(level, (int, float)):
                    continue
                paddle["scream"] = max(paddle["scream"], max(0.0, min(1.0, level)))
    except WebSocketDisconnect:
        manager.disconnect(websocket)


def print_startup_banner(lan_ip: str, other_ips: List[str]):
    print("\n=== Scream Pong server running (HTTP for laptops, HTTPS for phones) ===")
    print(f"  This laptop (left court):   http://{lan_ip}:{HTTP_PORT}/?side=left")
    print(f"  Other laptop (right court): http://{lan_ip}:{HTTP_PORT}/?side=right")
    print(f"  Phone controller (p1):      https://{lan_ip}:{HTTPS_PORT}/controller?player=1")
    print(f"  Phone controller (p2):      https://{lan_ip}:{HTTPS_PORT}/controller?player=2")
    print("  (Or just scan the QR code shown on each laptop's display page --")
    print("   it already points at the HTTPS controller URL above.)")
    if other_ips:
        print(f"  If a device can't connect on {lan_ip}, this machine also answers on: "
              f"{', '.join(other_ips)} -- try one of those instead.")
    print("  (All devices must be on the same Wi-Fi network as this machine)")
    print("  Phones will show a security warning for the self-signed HTTPS cert --")
    print("  click through it once (e.g. 'Advanced > Proceed'). Laptops load over")
    print("  plain HTTP so they never see that warning at all.")
    print(f"  Tick rate: {TICK_HZ}Hz | Broadcast rate: {TICK_HZ / TICKS_PER_BROADCAST:.0f}Hz "
          f"(tune with TICK_HZ / BROADCAST_HZ env vars)")
    print(f"  uvloop: {'on' if uvloop else 'off (pip install uvloop for a speed boost)'}")
    print()


async def main():
    ensure_self_signed_cert(CERT_FILE, KEY_FILE, get_all_local_ips())

    lan_ip = get_lan_ip()
    other_ips = [ip for ip in get_all_local_ips() if ip != lan_ip]
    print_startup_banner(lan_ip, other_ips)

    # Started once, here -- not inside a FastAPI lifespan hook -- because
    # the SAME `app` object below is handed to two separate uvicorn Servers.
    # A lifespan hook fires once per listener that starts, which would spin
    # up two competing game_loop tasks (double-speed physics, double
    # broadcasts) if it lived on the app instead.
    game_task = asyncio.create_task(game_loop())

    # loop="asyncio" tells uvicorn to use the event loop we're already
    # running (and the policy/uvloop we already installed above) instead of
    # trying to set one up itself. We call server.serve() directly below
    # (not server.run()), which never touches process signal handlers, so
    # the two servers don't fight over SIGINT/SIGTERM; asyncio.run() still
    # handles Ctrl+C cleanly for the whole process.
    http_server = uvicorn.Server(uvicorn.Config(
        app,
        host="0.0.0.0",
        port=HTTP_PORT,
        log_level="info",
        access_log=True,
        loop="asyncio",
    ))
    https_server = uvicorn.Server(uvicorn.Config(
        app,
        host="0.0.0.0",
        port=HTTPS_PORT,
        log_level="info",
        # Verbose on purpose right now: with access_log=True you'll see every
        # incoming request AND any TLS handshake failure in the console (a
        # phone that can't get past the self-signed cert never even reaches
        # our own "[+] Client connected" print, since that only fires after a
        # successful handshake -- these logs are what show a connection
        # attempt that died before that point). Once things are working
        # reliably you can drop this to log_level="warning", access_log=False
        # to cut console I/O on weak hardware.
        access_log=True,
        ssl_certfile=CERT_FILE,
        ssl_keyfile=KEY_FILE,
        loop="asyncio",
    ))

    try:
        await asyncio.gather(http_server.serve(), https_server.serve())
    finally:
        game_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass