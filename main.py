import asyncio
import json
import socket
from contextlib import asynccontextmanager
from typing import List

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

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

    async def broadcast(self, message: str):
        if not self.active_connections:
            return
        tasks = [connection.send_text(message) for connection in self.active_connections]
        await asyncio.gather(*tasks, return_exceptions=True)


manager = ConnectionManager()


def reset_ball(serve_towards: int = 1):
    """serve_towards: 1 -> ball moves toward player 1 (negative vx), 2 -> toward player 2."""
    game_state["ball"]["x"] = COURT_WIDTH / 2
    game_state["ball"]["y"] = CANVAS_HEIGHT / 2
    game_state["ball"]["vx"] = -6 if serve_towards == 1 else 6
    game_state["ball"]["vy"] = 5 if game_state["ball"]["vy"] >= 0 else -5


async def game_loop():
    """Authoritative 60 Hz physics loop. This is the ONLY place game state changes."""
    while True:
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

        await manager.broadcast(json.dumps(game_state))
        await asyncio.sleep(1 / 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(game_loop())
    lan_ip = get_lan_ip()
    print("\n=== Scream Pong server running ===")
    print(f"  This laptop (left court):   http://{lan_ip}:8000/?side=left")
    print(f"  Other laptop (right court): http://{lan_ip}:8000/?side=right")
    print(f"  Phone controller (p1):      http://{lan_ip}:8000/controller?player=1")
    print(f"  Phone controller (p2):      http://{lan_ip}:8000/controller?player=2")
    print("  (All devices must be on the same Wi-Fi network as this machine)\n")
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
    uvicorn.run(app, host="0.0.0.0", port=8000)