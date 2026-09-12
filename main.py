import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager
from typing import List

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

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
PADDLE_HEIGHT = 100
PADDLE_SPEED = 8
PADDLE_MARGIN = 30          # distance of each paddle from its outer wall

PADDLE1_X = PADDLE_MARGIN
PADDLE2_X = COURT_WIDTH - PADDLE_MARGIN - PADDLE_WIDTH

game_state = {
    "court": {"width": COURT_WIDTH, "height": CANVAS_HEIGHT, "screen_width": CANVAS_WIDTH},
    "ball": {"x": COURT_WIDTH / 2, "y": CANVAS_HEIGHT / 2, "size": BALL_SIZE, "vx": 6, "vy": 5},
    "paddles": {
        "p1": {"x": PADDLE1_X, "y": CANVAS_HEIGHT / 2 - PADDLE_HEIGHT / 2},
        "p2": {"x": PADDLE2_X, "y": CANVAS_HEIGHT / 2 - PADDLE_HEIGHT / 2},
    },
    "paddle_size": {"width": PADDLE_WIDTH, "height": PADDLE_HEIGHT},
    "score": {"p1": 0, "p2": 0},
}

# Reused every broadcast instead of allocating a fresh dict each tick.
# Coordinates are rounded to whole pixels for the wire -- the client only
# ever draws integer pixels anyway, and shorter numbers serialize faster
# and take less bandwidth than raw floats like 505.32999999999993.
_wire = {
    "court": game_state["court"],
    "ball": {"x": 0, "y": 0, "size": BALL_SIZE},
    "paddles": {"p1": {"x": PADDLE1_X, "y": 0}, "p2": {"x": PADDLE2_X, "y": 0}},
    "paddle_size": game_state["paddle_size"],
    "score": game_state["score"],
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


def step_physics():
    ball = game_state["ball"]
    p1 = game_state["paddles"]["p1"]
    p2 = game_state["paddles"]["p2"]

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
        and ball["y"] <= p1["y"] + PADDLE_HEIGHT
    ):
        ball["x"] = p1["x"] + PADDLE_WIDTH
        ball["vx"] *= -1

    # Paddle 2 collision (right wall of the court)
    if (
        ball["vx"] > 0
        and ball["x"] + BALL_SIZE >= p2["x"]
        and ball["x"] <= p2["x"] + PADDLE_WIDTH
        and ball["y"] + BALL_SIZE >= p2["y"]
        and ball["y"] <= p2["y"] + PADDLE_HEIGHT
    ):
        ball["x"] = p2["x"] - BALL_SIZE
        ball["vx"] *= -1

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

    wb = _wire["ball"]
    wb["x"] = int(ball["x"])
    wb["y"] = int(ball["y"])

    _wire["paddles"]["p1"]["y"] = int(p1["y"])
    _wire["paddles"]["p2"]["y"] = int(p2["y"])

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(game_loop())
    lan_ip = get_lan_ip()
    print("\n=== Scream Pong server running ===")
    print(f"  This laptop (left court):   http://{lan_ip}:8000/?side=left")
    print(f"  Other laptop (right court): http://{lan_ip}:8000/?side=right")
    print(f"  Phone controller (p1):      http://{lan_ip}:8000/controller?player=1")
    print(f"  Phone controller (p2):      http://{lan_ip}:8000/controller?player=2")
    print("  (All devices must be on the same Wi-Fi network as this machine)")
    print(f"  Tick rate: {TICK_HZ}Hz | Broadcast rate: {TICK_HZ / TICKS_PER_BROADCAST:.0f}Hz "
          f"(tune with TICK_HZ / BROADCAST_HZ env vars)")
    print(f"  uvloop: {'on' if uvloop else 'off (pip install uvloop for a speed boost)'}")
    print()
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def get_main_display():
    with open("index.html", "r", encoding="utf-8") as file:
        html_content = file.read()
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
            if action not in ("up", "down") or player not in (1, 2):
                continue

            paddle = game_state["paddles"]["p1"] if player == 1 else game_state["paddles"]["p2"]
            if action == "up":
                paddle["y"] = max(0, paddle["y"] - PADDLE_SPEED)
            else:
                paddle["y"] = min(CANVAS_HEIGHT - PADDLE_HEIGHT, paddle["y"] + PADDLE_SPEED)
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    print("Host loaded at http://localhost:8000/")
    # access_log=False and a quieter log level cut down on console I/O,
    # which is a real (if small) CPU cost on weak hardware under load.
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning", access_log=False)