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
    html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>Pong - Main Display</title>
    <style>
        body { background: #000; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        canvas { background: #111; border: 2px solid #555; }
        #status { position: fixed; top: 10px; left: 10px; color: #0f0; font-family: monospace; }
    </style>
</head>
<body>
    <div id="status">connecting...</div>
    <canvas id="game" width="800" height="600"></canvas>

    <script>
        const canvas = document.getElementById('game');
        const ctx = canvas.getContext('2d');
        const status = document.getElementById('status');

        const ws = new WebSocket(`ws://${location.host}/ws/display`);

        ws.onopen = () => { status.textContent = 'connected'; };
        ws.onclose = () => { status.textContent = 'disconnected'; };
        ws.onerror = (err) => { status.textContent = 'error'; console.error('WebSocket error:', err); };

        ws.onmessage = (event) => {
            const state = JSON.parse(event.data);
            draw(state);
        };

        function draw(state) {
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            // Ball
            ctx.fillStyle = 'white';
            ctx.fillRect(state.ball.x, state.ball.y, 10, 10);

            // Paddles
            ctx.fillRect(10, state.paddles.p1, 10, 100);
            ctx.fillRect(canvas.width - 20, state.paddles.p2, 10, 100);

            // Score
            ctx.font = '24px monospace';
            ctx.fillText(`${state.score.p1}  -  ${state.score.p2}`, canvas.width / 2 - 30, 30);

            // Center line
            ctx.setLineDash([5, 10]);
            ctx.beginPath();
            ctx.moveTo(canvas.width / 2, 0);
            ctx.lineTo(canvas.width / 2, canvas.height);
            ctx.strokeStyle = '#444';
            ctx.stroke();
        }
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


@app.get("/controller")
async def get_controller():
    html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>Pong - Controller</title>
    <style>
        body { background: #000; color: #eee; font-family: monospace; display: flex;
               flex-direction: column; align-items: center; justify-content: center; height: 100vh; margin: 0; }
        button { width: 150px; height: 80px; font-size: 20px; margin: 10px; }
        #status { position: fixed; top: 10px; left: 10px; color: #0f0; }
    </style>
</head>
<body>
    <div id="status">connecting...</div>
    <h2>Controller (player 1)</h2>
    <button id="up">UP</button>
    <button id="down">DOWN</button>

    <script>
        const status = document.getElementById('status');
        const ws = new WebSocket(`ws://${location.host}/ws/controller`);

        ws.onopen = () => { status.textContent = 'connected'; };
        ws.onclose = () => { status.textContent = 'disconnected'; };
        ws.onerror = () => { status.textContent = 'error'; };

        function send(direction) {
            if (ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ action: direction }));
            }
        }

        document.getElementById('up').addEventListener('click', () => send('up'));
        document.getElementById('down').addEventListener('click', () => send('down'));

        window.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowUp') send('up');
            if (e.key === 'ArrowDown') send('down');
        });
    </script>
</body>
</html>
"""
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