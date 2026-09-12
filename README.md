<img width="1280" height="640" alt="image" src="https://github.com/user-attachments/assets/30822118-5426-4152-9aaa-e67f12caccb8" />

# Scream Pong

### Demo

View Our Poster [Here](https://death7654.github.io/) to see a full list of features and how it works!!!

A two-laptop, split-screen Pong where the "court" is twice as wide as either
screen — the ball physically travels from one laptop to the other. Each
player's paddle is controlled by their **phone**, and the harder you scream
into your phone's mic, the bigger your paddle grows.

> Two laptops sit side by side, screens touching, so the court looks like one
> continuous playing field. Each player scans a QR code with their phone to
> pull up a controller — buttons, tilt controls, or their microphone.

---

## Table of Contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
  - [High-level overview](#high-level-overview)
  - [One process, two listeners](#one-process-two-listeners)
  - [Authoritative server, dumb clients](#authoritative-server-dumb-clients)
  - [The shared court coordinate system](#the-shared-court-coordinate-system)
  - [The game loop](#the-game-loop)
  - [Networking: broadcast decoupled from physics](#networking-broadcast-decoupled-from-physics)
  - [The scream mechanic](#the-scream-mechanic)
  - [Powerups](#powerups)
  - [Controllers: buttons vs. motion](#controllers-buttons-vs-motion)
  - [TLS: why HTTP *and* HTTPS](#tls-why-http-and-https)
- [Project structure](#project-structure)
- [Setup & installation](#setup--installation)
- [Running the game](#running-the-game)
- [Configuration](#configuration)
- [Screenshots](#screenshots)
- [Troubleshooting](#troubleshooting)
- [Team](#team)

---

## How it works

1. Start `main.py` on **one** laptop — this is the server. It also renders
   the left half of the court.
2. A second laptop opens the same server's address in a browser with
   `?side=right` — it renders the right half of the court. Nothing else runs
   on it; it's just another browser tab pointed at laptop #1.
3. Each player scans a QR code (shown on their laptop's screen) with their
   phone, which opens a mobile controller page served over HTTPS.
4. Players pick **Button Controls** (tap UP/DOWN) or **Motion Controls**
   (tilt the phone to move, swing it to hit) — and optionally enable the mic
   so screaming grows their paddle.
5. The ball flies back and forth across the "seam" between the two laptop
   screens as if it were one continuous table.

---

## Architecture

### High-level overview

```
                     ┌─────────────────────────────┐
                     │      main.py (FastAPI)       │
                     │                              │
   Laptop (left) ───▶│  GET /            (HTTP)     │
   Laptop (right) ──▶│  GET /?side=right (HTTP)     │
                     │  GET /controller  (HTTPS)     │
                     │                              │◀── Phone P1 (HTTPS)
                     │  WS  /ws/{client_id}          │◀── Phone P2 (HTTPS)
                     │       ▲          ▲            │
                     │       │          │            │
                     │  ┌────┴────┐┌────┴────┐       │
                     │  │game_loop││Connection│       │
                     │  │ (60 Hz) ││ Manager  │       │
                     │  └────┬────┘└────┬────┘       │
                     │       │          │            │
                     │  ┌────▼──────────▼───┐         │
                     │  │   game_state       │         │
                     │  │ (ball/paddles/etc) │         │
                     │  └────────────────────┘         │
                     └─────────────────────────────┘
```

Every device — both laptop displays and both phone controllers — connects to
the **same FastAPI app, in the same process**, over a single generic
WebSocket endpoint (`/ws/{client_id}`). There is exactly one `game_state`
dict and one physics loop; nothing is duplicated per device.

### One process, two listeners

`main.py` starts **two** `uvicorn.Server` instances against the same `app`
object, gathered together with `asyncio.gather`:

| Listener | Port | Protocol | Who connects | Why |
|---|---|---|---|---|
| HTTP | `8000` (default) | plain HTTP/WS | The two laptop displays | No self-signed cert warning to click through |
| HTTPS | `8443` (default) | HTTPS/WSS | The two phone controllers | Phones only expose `DeviceOrientation`/`DeviceMotion` (used for tilt controls) on a secure context, and a `wss://` socket is required once a page is loaded over `https://` (mixed-content rules) |

Because both listeners share one `app`, one `ConnectionManager`, and one
`game_state`, a laptop's keyboard and a phone's tilt controller can drive the
exact same paddle without the server ever needing to know which "kind" of
device sent a message.

The physics loop (`game_loop()`) is started once, manually, *before* either
server starts serving — not inside a FastAPI lifespan hook. A lifespan hook
fires once per listener, which would have spun up **two** competing game
loops (double-speed physics, doubled broadcasts) had it lived on the app.

### Authoritative server, dumb clients

The server is the single source of truth. `step_physics()` is the *only*
place `game_state` changes — ball position, paddle position, scores,
powerups, and scream-driven paddle height are all computed server-side.
Clients (both laptops and phones) never simulate anything; they just:

- **send input** (`up` / `down` / `set` / `scream`), and
- **render whatever state they're told** about over the WebSocket.

This keeps both laptop screens and both phones perfectly in sync, and means
a slow/laggy device only ever affects itself, never the shared simulation.

### The shared court coordinate system

The "court" is one continuous playing field, twice as wide as a single
screen:

```
world-x:     0 ─────────────── 800 ─────────────── 1600
             |     left half        |    right half     |
             |   (laptop #1 draws)  |  (laptop #2 draws) |
```

- `CANVAS_WIDTH` (800px) is the width of one screen/court-half.
- `COURT_WIDTH = CANVAS_WIDTH * 2` (1600px) is the full court.
- The ball's `x`/`y` are tracked **once**, in world coordinates, inside
  `game_state`.
- Each display just clips to its own half (`?side=left` draws world-x
  `[0, 800)`, `?side=right` draws `[800, 1600)`), which is what makes the
  ball appear to travel seamlessly from one laptop's screen onto the
  other's.

### The game loop

`game_loop()` runs at a fixed tick rate (`TICK_HZ`, default 60Hz):

1. `step_physics()` — move the ball, resolve wall/paddle/powerup
   collisions, decay scream charge into paddle height, and register
   scoring.
2. Every `TICKS_PER_BROADCAST` ticks, serialize the state and broadcast it.
3. Sleep until the *next scheduled tick time* (not just "sleep `TICK_DT`"),
   so a slow tick doesn't cause drift to accumulate — and if the loop falls
   behind, it resyncs to "now" instead of bursting through a pile of queued
   ticks (which would spike CPU on already-struggling hardware).

### Networking: broadcast decoupled from physics

Physics and network broadcast rate are **independently configurable**
(`TICK_HZ` vs `BROADCAST_HZ`), so low-end hardware or weak Wi-Fi can drop the
number of network messages per second without sacrificing simulation
accuracy.

Broadcasting itself is fire-and-forget: each client's `send_text` is
scheduled as its own `asyncio` task (with a timeout, dropping unresponsive
clients) rather than `await`-ing every client's send before the loop can
tick again. Previously, one laggy phone or a flaky laptop Wi-Fi hop could
stall the entire game for both players; now the physics loop always runs at
full speed regardless of any individual client's network conditions.

Wire messages are also kept small on purpose:
- coordinates are rounded to whole pixels before serialization,
- a single reusable dict (`_wire`) is mutated and re-serialized each tick
  instead of allocating a fresh one,
- `orjson` is used instead of the stdlib `json` module when available (falls
  back silently if not installed).

### The scream mechanic

Each phone controller can (with permission) listen to the microphone and
report a loudness "level" between `0.0` (quiet) and `1.0` (screaming)
several times a second, via `{"action": "scream", "level": ..., "player": ...}`.

The server tracks a per-paddle scream **charge** that behaves like a
peak-hold VU meter:
- an incoming `scream` message can only push that paddle's charge **up**
  instantly, so a scream registers right away;
- every physics tick, `step_physics()` decays the charge back down
  (`charge *= 0.985`), so it takes a couple of seconds of silence to fully
  fade;
- paddle height is derived fresh from the current charge every tick, maxing
  out at `+50%` of base height (`PADDLE_SCREAM_MAX_BONUS`) at full charge.

### Powerups

A powerup spawns in the middle third of the court once every 3–6 paddle
hits (re-rolled after each pickup). It's picked up when the **ball** touches
it (circle/rectangle collision), and either speeds the ball up (`fast`,
×1.35) or slows it down (`slow`, ×0.72). Resulting speed is clamped between
`BALL_MIN_SPEED` and `BALL_MAX_SPEED` so repeated powerups can't spiral the
ball into being unhittable or boring.

### Controllers: buttons vs. motion

`controller.html` is a single page with three screens (mode select, button
mode, motion mode), all driving the same WebSocket protocol:

| Mode | Input | Message sent |
|---|---|---|
| Button | Holding UP/DOWN taps/keys | `{"action": "up" \| "down", "player": N}` — nudges the paddle a fixed step each message |
| Motion (tilt) | Phone's `DeviceOrientation` beta angle, throttled to 20Hz | `{"action": "set", "value": 0.0–1.0, "player": N}` — jumps the paddle straight to an absolute position, since a tilt angle already represents "how far up/down" rather than a direction to nudge |
| Either | Mic loudness | `{"action": "scream", "level": 0.0–1.0, "player": N}` |

The two message shapes (`up`/`down` vs `set`) exist because a laptop's own
keyboard and a phone's button-tap both want to "nudge" a paddle repeatedly
while held, whereas a phone's tilt sensor already knows the paddle's target
position outright.

### TLS: why HTTP *and* HTTPS

On first run, `main.py` generates a throwaway **self-signed** TLS
certificate (`cert.pem` / `key.pem`) covering `localhost`, `127.0.0.1`, and
every LAN-facing IPv4 address it can find on the machine (not just one
best-guess address — a laptop with Wi-Fi + Ethernet + a VPN/Docker adapter
can otherwise mint a cert that doesn't cover the address other devices
actually use to reach it, which shows up as "the page loads, but the
WebSocket immediately disconnects and keeps retrying"). The cert is
regenerated automatically if it's missing, expired, or no longer covers the
machine's current IPs (e.g. it joined a new network since last run).

Phones will see a one-time browser warning for the self-signed cert (click
"Advanced → Proceed"); laptops load over plain HTTP and never see it.

---

## Project structure

```
.
├── main.py                        # FastAPI server: physics, WebSocket protocol, HTTP/HTTPS listeners, TLS bootstrap
├── pages/
│   ├── index/
│   │   ├── index.html             # Laptop display markup (game canvas, QR code, keyboard controls)
│   │   ├── index.css              # Styles for index.html
│   │   └── index.js               # Display logic: WebSocket state, canvas rendering, QR/setup flow
│   └── controller/
│       ├── controller.html        # Phone controller markup (button mode / motion mode / scream meter)
│       ├── controller.css         # Styles for controller.html
│       └── controller.js          # Controller logic: button/motion input, scream meter, WebSocket send
├── requirements.txt               # Python dependencies
├── cert.pem / key.pem             # Auto-generated self-signed TLS cert+key (regenerated as needed)
└── README.md
```

Each page's HTML, CSS, and JS live together in their own folder under `pages/`. `main.py` reads `pages/index/index.html` and `pages/controller/controller.html` at `/` and `/controller` as before, and mounts the whole `pages/` directory at `/static` (via FastAPI's `StaticFiles`) so each page's `<link>`/`<script src>` tags can pull in its own CSS/JS, e.g. `/static/index/index.css` and `/static/controller/controller.js`.

## Setup & installation

**Requirements:** Python 3.9+, and both laptops + both phones on the **same
Wi-Fi network**.

```bash
git clone <this-repo>
cd <this-repo>
pip install -r requirements.txt
```

Optional (recommended) speed-ups — installed automatically if present,
never required:

```bash
pip install uvloop orjson   # uvloop is Linux/macOS only
```

## Running the game

**On laptop #1 (the server):**

```bash
python main.py
```

This prints a startup banner with every URL you need, e.g.:

```
=== Scream Pong server running (HTTP for laptops, HTTPS for phones) ===
  This laptop (left court):   http://<lan-ip>:8000/?side=left
  Other laptop (right court): http://<lan-ip>:8000/?side=right
  Phone controller (p1):      https://<lan-ip>:8443/controller?player=1
  Phone controller (p2):      https://<lan-ip>:8443/controller?player=2
  (Or just scan the QR code shown on each laptop's display page --
   it already points at the HTTPS controller URL above.)
```

**On laptop #2:** open `http://<lan-ip>:8000/?side=right` in a browser.

**On each phone:** scan the QR code shown on-screen (or open the
`https://.../controller?player=N` link directly), click through the
self-signed-cert warning once, then pick Button or Motion controls.

## Configuration

Everything is tunable via environment variables — no code edits needed:

| Variable | Default | Purpose |
|---|---|---|
| `TICK_HZ` | `60` | Physics simulation rate |
| `BROADCAST_HZ` | same as `TICK_HZ` | Network update rate (can be lower than `TICK_HZ` on weak Wi-Fi/CPUs) |
| `HTTP_PORT` | `8000` | Port for laptop displays |
| `HTTPS_PORT` | `8443` | Port for phone controllers |

Example, for a low-end machine:

```bash
BROADCAST_HZ=20 python main.py
```

## Screenshots
<img width="500" height="241" alt="image" src="https://github.com/user-attachments/assets/f223a3a1-bb34-4f0b-b785-bbc4ca00d7fb" />
<img width="500" height="246" alt="image" src="https://github.com/user-attachments/assets/145ae34a-bbe8-4168-bc7b-0bd2186a8eb9" />
<img width="250" height="486" alt="WhatsApp Image 2026-09-12 at 3 17 13 PM" src="https://github.com/user-attachments/assets/04545452-215a-44b1-b2d3-27a292ba00a7" />
<img width="250" height="452" alt="image" src="https://github.com/user-attachments/assets/79a345b6-3f79-46e4-9c4a-a413e19a3608" />


## Troubleshooting

- **Phone connects, then immediately disconnects/retries:** usually a TLS
  cert/IP mismatch — delete `cert.pem`/`key.pem` and restart so a fresh cert
  covering your current network is generated.
- **Scary `ConnectionResetError [WinError 10054]` on Windows:** cosmetic —
  caused by a known asyncio/Windows event-loop quirk on abrupt disconnects
  (tab closed, phone locked). `main.py` already switches to the Selector
  event loop on Windows to suppress it.
- **Console too noisy:** drop `access_log=True` / `log_level="info"` to
  `access_log=False` / `log_level="warning"` on the HTTPS server in
  `main.py` once things are working reliably.


## Team
- Rosmi Reji
- Robinson George Arysseril

---

Made with ❤️ at TinkerHub Useless Projects

![Static Badge](https://img.shields.io/badge/TinkerHub-24?color=%23000000&link=https%3A%2F%2Fwww.tinkerhub.org%2F)
![Static Badge](https://img.shields.io/badge/UselessProjects--26-26?link=https%3A%2F%2Ftinkerhub.org%2Fevents%2F1M8ORET9A1%2Fuseless-projects-3.0)
