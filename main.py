import asyncio
import json
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from typing import List

# Server configuration & state
CANVAS_WIDTH = 800
CANVAS_HEIGHT = 600
BALL_SIZE = 10
PADDLE_HEIGHT = 100
PADDLE_SPEED = 8

game_state = {
    "ball": {"x": 400, "y": 300, "vx": 5, "vy": 5},
    "paddles": {"p1": 250, "p2": 250},
    "score": {"p1": 0, "p2": 0}
}


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
        tasks = [connection.send_text(message) for connection in self.active_connections]
        # Sends to all clients concurrently without stalling the loop
        await asyncio.gather(*tasks, return_exceptions=True)


manager = ConnectionManager()


async def game_loop():
    """Authoritative 60 Hz physics loop."""
    while True:
        # Move ball
        game_state["ball"]["x"] += game_state["ball"]["vx"]
        game_state["ball"]["y"] += game_state["ball"]["vy"]

        # Bounce off top & bottom boundaries
        if game_state["ball"]["y"] <= 0 or game_state["ball"]["y"] >= CANVAS_HEIGHT - BALL_SIZE:
            game_state["ball"]["vy"] *= -1

        # Bounce off left & right boundaries (Placeholder physics)
        if game_state["ball"]["x"] <= 0 or game_state["ball"]["x"] >= CANVAS_WIDTH - BALL_SIZE:
            game_state["ball"]["vx"] *= -1

        # Broadcast update to connected clients
        if manager.active_connections:
            await manager.broadcast(json.dumps(game_state))

        await asyncio.sleep(1 / 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(game_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def get_main_display():
    with open("index.html", "r", encoding="utf-8") as file:
      content = file.read()
      print(content) 
    html_content = content
    return HTMLResponse(content=html_content)


@app.get("/controller")
async def get_controller():
    with open('controller.html', 'r', encoding="utf-8") as file:
      content = file.read()
    html_content = content
    return HTMLResponse(content=html_content)


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            print(f"[{client_id}] Data received: {data}")

            # Basic controller input handling (client_id == "controller" moves p1)
            if client_id == "controller":
                try:
                    msg = json.loads(data)
                    action = msg.get("action")
                    if action == "up":
                        game_state["paddles"]["p1"] = max(0, game_state["paddles"]["p1"] - PADDLE_SPEED)
                    elif action == "down":
                        game_state["paddles"]["p1"] = min(
                            CANVAS_HEIGHT - PADDLE_HEIGHT, game_state["paddles"]["p1"] + PADDLE_SPEED
                        )
                except json.JSONDecodeError:
                    pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    print("Host loaded at http://localhost:8000/")
    uvicorn.run(app, host="0.0.0.0", port=8000)