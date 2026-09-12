

// ============================================================
// URL PARAMETERS
// ============================================================

const params =
    new URLSearchParams(
        window.location.search
    );


const mode =
    params.get("mode");

// QR phone-handshake state
let qrWaitingForPhone = false;
let initialPaddleY = null;


// ============================================================
// CONTROL METHOD SELECTION
// ============================================================
//
// NOTE: this used to also wire up a "choose laptop keyboard vs phone"
// overlay (#controlOverlay / #controlTitle / #controlSubtitle /
// #laptopControl / #phoneControl), but that markup doesn't exist in this
// page anymore. Looking those elements up with getElementById() returned
// null, and setting `.onclick` on null threw a TypeError -- which, being
// an uncaught error at the top level of this <script> tag, silently
// stopped the ENTIRE script right there. That's why the laptop displays
// never got as far as opening their WebSocket (further down this file)
// even though the connection code itself was fine -- the phone controller
// lives in a separate file (controller.html) so it was never affected.
//
// The only thing actually needed here is the watermark text, so that's
// all this block does now.
if (mode !== "controller") {

    const side =
        params.get("side") === "right"
            ? "right"
            : "left";

    const backgroundTitle =
        document.getElementById("backgroundTitle");

    // Player 1's laptop shows SHARED.
    // Player 2's laptop shows SCREAM.
    if (backgroundTitle) {
        backgroundTitle.textContent =
            side === "left" ? "SHARED" : "SCREAM";
    }
}


// ============================================================
// PHONE QR
// ============================================================

function showPhoneQR(player) {

    const qrSection =
        document.getElementById("qrSection");

    const qrContainer =
        document.getElementById("qrcode");

    const qrText =
        document.getElementById("qrText");

    qrContainer.innerHTML = "";

    /*
     * controller.html is the separate phone controller.
     * The phone must be on the same Wi-Fi/network as the game server.
     */
    const controllerURL =
        `${window.location.protocol}//` +
        `${window.location.host}` +
        `/controller.html?player=${player}`;

    new QRCode(
        qrContainer,
        {
            text: controllerURL,
            width: 145,
            height: 145,
            correctLevel: QRCode.CorrectLevel.H
        }
    );

    qrText.textContent =
        "Scan this code with the player's phone";

    qrSection.style.display = "block";
}


// ============================================================
// PHONE CONTROLLER
// ============================================================

if (mode === "controller") {


    // Hide laptop game

    document.getElementById(
        "gamePage"
    ).style.display = "none";


    // Show controller

    document.getElementById(
        "controller"
    ).style.display = "flex";


    // --------------------------------------------------------
    // PLAYER
    // --------------------------------------------------------

    let player =
        params.get("player");


    if (player !== "2") {

        player = "1";

    }


    document.getElementById(
        "phonePlayer"
    ).textContent = player;


    // --------------------------------------------------------
    // PHONE WEBSOCKET
    // --------------------------------------------------------

    const phoneStatus =
        document.getElementById(
            "phoneStatus"
        );


    let phoneSocket;


    function connectPhone() {


        phoneSocket =
            new WebSocket(

                `ws://${location.host}/ws/controller-${player}`

            );


        phoneSocket.onopen = function() {

            phoneStatus.textContent =
                "🟢 CONNECTED";

        };


        phoneSocket.onclose = function() {

            phoneStatus.textContent =
                "🔴 DISCONNECTED";

            setTimeout(
                connectPhone,
                1000
            );

        };


        phoneSocket.onerror = function() {

            phoneSocket.close();

        };

    }


    connectPhone();


    // --------------------------------------------------------
    // SEND COMMAND
    // --------------------------------------------------------

    function sendCommand(action) {


        if (

            phoneSocket &&

            phoneSocket.readyState ===
            WebSocket.OPEN

        ) {


            phoneSocket.send(

                JSON.stringify({

                    action: action,

                    player: Number(player)

                })

            );

        }

    }


    // --------------------------------------------------------
    // BUTTONS
    // --------------------------------------------------------

    const upButton =
        document.getElementById(
            "upButton"
        );


    const downButton =
        document.getElementById(
            "downButton"
        );


    // UP

    upButton.addEventListener(
        "touchstart",
        function(e) {

            e.preventDefault();

            sendCommand("up");

        }
    );


    upButton.addEventListener(
        "touchend",
        function(e) {

            e.preventDefault();

            sendCommand("stop");

        }
    );


    // DOWN

    downButton.addEventListener(
        "touchstart",
        function(e) {

            e.preventDefault();

            sendCommand("down");

        }
    );


    downButton.addEventListener(
        "touchend",
        function(e) {

            e.preventDefault();

            sendCommand("stop");

        }
    );


    // Mouse support

    upButton.addEventListener(
        "mousedown",
        () => sendCommand("up")
    );


    upButton.addEventListener(
        "mouseup",
        () => sendCommand("stop")
    );


    downButton.addEventListener(
        "mousedown",
        () => sendCommand("down")
    );


    downButton.addEventListener(
        "mouseup",
        () => sendCommand("stop")
    );

}



// ============================================================
// LAPTOP GAME
// ============================================================

else {


    // --------------------------------------------------------
    // SIDE
    // --------------------------------------------------------

    const side =
        params.get("side") === "right"
            ? "right"
            : "left";


    const myPlayer =
        side === "left"
            ? 1
            : 2;


    // --------------------------------------------------------
    // CONTROLLER URL / QR HELPER
    // --------------------------------------------------------
    // Used only by the setup screen's QR code, shown before the game
    // starts. The in-game QR panel is intentionally never shown.
    // Always HTTPS regardless of what protocol this display page
    // itself loaded over: phones need a secure context for motion
    // controls, and /controller (not the old ?mode=controller button
    // fallback) is the page that actually supports them.

    function buildControllerURL() {
        return (
            `https://` +
            `${window.location.hostname}` +
            `:${window.HTTPS_PORT}` +
            `/controller?player=${myPlayer}`
        );
    }

    function renderControllerQR(containerId, textId) {
        const container = document.getElementById(containerId);
        const textEl = document.getElementById(textId);
        if (!container) return;

        container.innerHTML = "";
        const url = buildControllerURL();

        new QRCode(container, {
            text: url,
            width: container.clientWidth || 145,
            height: container.clientHeight || 145,
            correctLevel: QRCode.CorrectLevel.H
        });

        if (textEl) textEl.textContent = url;
    }


    // --------------------------------------------------------
    // SETUP FLOW (choose controls -> connect phone -> start)
    // --------------------------------------------------------
    // Every device runs this independently: each laptop's own page
    // load shows its own "choose keyboard or phone" screen for
    // its own player, then reveals the actual court (#gamePage) and
    // opens its websocket only once that player taps Start Game.

    let controlMethod = "keyboard";
    let gameStarted = false;

    const setupScreens = {
        mode: document.getElementById("setupScreenMode"),
        phone: document.getElementById("setupScreenPhone"),
        ready: document.getElementById("setupScreenReady"),
    };

    function showSetupScreen(name) {
        Object.values(setupScreens).forEach(
            el => el.classList.remove("active")
        );
        setupScreens[name].classList.add("active");
    }

    document.getElementById("chooseKeyboard").addEventListener(
        "click",
        () => {
            controlMethod = "keyboard";
            showSetupScreen("ready");
        }
    );

    document.getElementById("choosePhone").addEventListener(
        "click",
        () => {
            controlMethod = "phone";
            renderControllerQR("setupQrcode", "setupQrText");
            showSetupScreen("phone");
        }
    );

    // The server has no way to confirm a specific phone paired with a
    // specific paddle, so this is a manual confirmation -- the player
    // scans, connects on their phone, then taps this once it says
    // "connected" over there.
    document.getElementById("phoneConnectedBtn").addEventListener(
        "click",
        () => {
            showSetupScreen("ready");
        }
    );

    document.getElementById("setupBackFromPhone").addEventListener(
        "click",
        () => showSetupScreen("mode")
    );

    document.getElementById("setupBackFromReady").addEventListener(
        "click",
        () => showSetupScreen("mode")
    );

    document.getElementById("startGameBtn").addEventListener(
        "click",
        () => {
            if (gameStarted) return;
            gameStarted = true;

            document.getElementById("setupOverlay").style.display = "none";
            document.getElementById("gamePage").style.display = "flex";

            // The canvas was hidden (gamePage was display:none) when
            // resizeCanvas() first ran further down, so clientWidth/
            // clientHeight would have read as 0 back then. Now that the
            // page is actually visible, measure it for real before the
            // first real draw.
            resizeCanvas();

            // The QR code is only needed during setup.
            // Once the game starts, never show the in-game QR panel.
            const qrSection = document.getElementById("qrSection");
            qrSection.style.display = "none";
            qrWaitingForPhone = false;

            connect();
            loop();
        }
    );


    // --------------------------------------------------------
    // CANVAS
    // --------------------------------------------------------

    const canvas =
        document.getElementById(
            "gameCanvas"
        );


    const ctx =
        canvas.getContext("2d");


    canvas.classList.add(

        side === "left"
            ? "side-left"
            : "side-right"

    );


    // --------------------------------------------------------
    // FULL-SCREEN CANVAS SIZING
    // --------------------------------------------------------
    // The canvas's CSS size is 100vw/100vh (see the stylesheet), so it
    // always fills the whole screen. This keeps the canvas's actual
    // drawing buffer (canvas.width/height, in real device pixels) in
    // sync with that CSS size -- including devicePixelRatio, so the
    // game stays sharp on high-DPI phone/laptop screens instead of
    // getting blurry or, worse, drawn at a stale resolution left over
    // from the last screen size. WIDTH/HEIGHT are read fresh from the
    // canvas inside draw() every frame, so nothing else needs to know
    // about resizes directly.

    function resizeCanvas() {

        // Cap DPR so very high-resolution screens don't force huge canvas
        // buffers and expensive redraws every frame.
        const dpr =
            Math.min(window.devicePixelRatio || 1, 2);

        const width =
            Math.round(canvas.clientWidth * dpr);

        const height =
            Math.round(canvas.clientHeight * dpr);

        if (canvas.width !== width || canvas.height !== height) {
            canvas.width = width;
            canvas.height = height;
        }

    }


    let resizeTimer = null;

    function scheduleResize() {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(resizeCanvas, 80);
    }

    window.addEventListener("resize", scheduleResize);
    window.addEventListener("orientationchange", scheduleResize);


    resizeCanvas();


    // --------------------------------------------------------
    // SIDE LABEL
    // --------------------------------------------------------

    document.getElementById(
        "sideLabel"
    ).textContent =

        side === "left"
            ? "PLAYER 1 • TOP"
            : "PLAYER 2 • BOTTOM";


    // --------------------------------------------------------
    // SERVER STATE
    // --------------------------------------------------------

    let latest = null;


    // Server world remains horizontal; we display it rotated 90 degrees.
    // Player 1's half-court is world-x [0, 800) and Player 2's is
    // [800, 1600) -- yOffset shifts whichever half this screen shows down
    // to local 0..800 (updated to the server's real screen_width as soon
    // as the first message arrives; 800 here is just a same-shaped guess
    // to draw something reasonable before that).
    let yOffset =
        side === "left"
            ? 0
            : 800;

    // Both screens show their OWN paddle at the BOTTOM of their OWN
    // canvas and the seam between the two courts at the TOP -- so the
    // ball crosses from the top of one screen straight into the top of
    // the other. World-x already increases from player 1's wall towards
    // player 2's wall, so player 1's local axis needs flipping to get
    // that "own paddle at the bottom" layout; player 2's doesn't.
    const flipPerp = side === "left";


    // --------------------------------------------------------
    // WEBSOCKET
    // --------------------------------------------------------

    const statusEl =
        document.getElementById(
            "status"
        );


    let ws;


    function connect() {


        ws =
            new WebSocket(

                `ws://${location.host}/ws/display-${side}`

            );


        ws.onopen = function() {


            statusEl.textContent =

                `● ONLINE • PLAYER ${myPlayer}`;


            statusEl.className =
                "connected";

        };


        ws.onclose = function() {


            statusEl.textContent =
                "● OFFLINE • RECONNECTING";


            statusEl.className =
                "disconnected";


            setTimeout(
                connect,
                1000
            );

        };


        ws.onerror = function() {

            ws.close();

        };


        ws.onmessage =
            function(event) {


                latest =
                    JSON.parse(
                        event.data
                    );

                // If the server exposes controller_connected, hide the QR
                // immediately when the phone connects after scanning.
                if (
                    qrWaitingForPhone &&
                    latest.controller_connected === true
                ) {
                    document.getElementById("qrSection").style.display = "none";
                    qrWaitingForPhone = false;
                }

                // Fallback for the current server: hide it after the first
                // real paddle movement from the phone.
                if (qrWaitingForPhone && latest.paddles) {
                    const myPaddle =
                        myPlayer === 1
                            ? latest.paddles.p1
                            : latest.paddles.p2;

                    if (myPaddle) {
                        if (initialPaddleY === null) {
                            initialPaddleY = myPaddle.y;
                        } else if (
                            Math.abs(myPaddle.y - initialPaddleY) > 1
                        ) {
                            document.getElementById("qrSection").style.display = "none";
                            qrWaitingForPhone = false;
                        }
                    }
                }


                if (

                    latest.court &&

                    latest.court.screen_width

                ) {


                    yOffset =

                        side === "left"
                            ? 0
                            : latest.court.screen_width;

                }


                document.getElementById(
                    "score1"
                ).textContent =
                    latest.score.p1;


                document.getElementById(
                    "score2"
                ).textContent =
                    latest.score.p2;

            };

    }


    // Opened by the startGameBtn handler above (see SETUP FLOW),
    // not automatically -- the court only starts talking to the
    // server once the player has actually pressed Start Game.


    // --------------------------------------------------------
    // SEND KEYBOARD COMMAND
    // --------------------------------------------------------

    function send(action) {


        if (

            ws &&

            ws.readyState ===
            WebSocket.OPEN

        ) {


            ws.send(

                JSON.stringify({

                    action: action,

                    player: myPlayer

                })

            );

        }

    }



    // --------------------------------------------------------
    // KEYBOARD
    // --------------------------------------------------------

    let keys = {};


    document.addEventListener(
        "keydown",
        function(e) {

            keys[e.key] = true;

        }
    );


    document.addEventListener(
        "keyup",
        function(e) {

            keys[e.key] = false;

        }
    );


    function pollKeys() {


        if (

            keys["w"] ||
            keys["W"] ||
            keys["ArrowUp"]

        ) {

            send("up");

        }


        if (

            keys["s"] ||
            keys["S"] ||
            keys["ArrowDown"]

        ) {

            send("down");

        }

    }


    setInterval(

        pollKeys,

        1000 / 30

    );



    // ========================================================
    // DRAW
    // ========================================================

    function draw() {


        // Read the canvas's REAL pixel size fresh every frame -- it
        // changes whenever resizeCanvas() runs (window resize, phone
        // rotation), so this can never be a stale const like it used
        // to be back when the canvas was a fixed 600x800.
        const WIDTH =
            canvas.width;

        const HEIGHT =
            canvas.height;


        // Clear

        ctx.clearRect(

            0,
            0,
            WIDTH,
            HEIGHT

        );


        if (!latest) {

            return;

        }


        // ----------------------------------------------------
        // WORLD -> SCREEN SCALE
        // ----------------------------------------------------
        // The server's world is a fixed size (see main.py: CANVAS_HEIGHT
        // is the paddle's up/down travel range, court.screen_width is
        // one player's half-court length) but this canvas now fills
        // whatever the real screen resolution is, on both axes
        // independently -- so every world coordinate needs scaling up
        // (or down) to land in the right spot. worldVert is the world's
        // "vertical/paddle-travel" axis, which after the 90-degree
        // rotation becomes this canvas's horizontal (X) axis; worldHoriz
        // is the world's "along the court" axis, which becomes this
        // canvas's vertical (Y) axis. Falling back to the server's
        // defaults (600/800) means the very first frame, before any
        // message has arrived, still draws something sane.
        const worldVert =
            (latest.court && latest.court.height) || 600;

        const worldHoriz =
            (latest.court && latest.court.screen_width) || 800;

        const scaleX = WIDTH / worldVert;
        const scaleY = HEIGHT / worldHoriz;


        // Maps a world-x position (which is 0 at player 1's own wall and
        // increases toward player 2's own wall) onto this screen's local
        // vertical axis, given the on-screen thickness of what's being
        // drawn (paddle thickness, ball/powerup diameter) -- both already
        // scaled by the caller into screen pixels. See flipPerp above for
        // why player 1's screen flips this and player 2's doesn't.
        function perpToScreen(worldX, thicknessScaled) {
            const local = (worldX - yOffset) * scaleY;
            return flipPerp ? (HEIGHT - local - thicknessScaled) : local;
        }


        // ----------------------------------------------------
        // DATA
        // ----------------------------------------------------

        const paddleSize =
            latest.paddle_size;


        const p1 =
            latest.paddles.p1;


        const p2 =
            latest.paddles.p2;


        const ball =
            latest.ball;


        // ----------------------------------------------------
        // BOUNCE-WALL BORDERS
        // ----------------------------------------------------
        // These are drawn INSIDE the canvas (in addition to the CSS
        // border-left/border-right on the canvas element itself) as a
        // glowing line right along world y=0 and y=CANVAS_HEIGHT -- the
        // exact pixels step_physics() bounces the ball off of. Drawing
        // them here as well keeps them pinned to the real collision
        // boundary even if a future change ever makes the CSS border
        // and the drawing buffer disagree by a pixel or two.

        // A soft violet-to-pink vertical gradient reads as a clean light
        // strip rather than an arcade neon glow -- same idea as the old
        // flat cyan line, just a calmer, more polished palette.
        // Cache the gradient; it only needs rebuilding when the canvas
        // dimensions change.
        if (
            !draw.wallGradient ||
            draw.wallGradientWidth !== WIDTH ||
            draw.wallGradientHeight !== HEIGHT
        ) {
            draw.wallGradient = ctx.createLinearGradient(0, 0, 0, HEIGHT);
            draw.wallGradient.addColorStop(0, "rgba(196, 181, 253, 0.85)");
            draw.wallGradient.addColorStop(0.5, "rgba(216, 180, 254, 0.6)");
            draw.wallGradient.addColorStop(1, "rgba(196, 181, 253, 0.85)");
            draw.wallGradientWidth = WIDTH;
            draw.wallGradientHeight = HEIGHT;
        }

        ctx.save();
        ctx.strokeStyle = draw.wallGradient;
        ctx.lineWidth = 3;
        ctx.shadowColor = "transparent";
        ctx.shadowBlur = 0;

        ctx.beginPath();
        ctx.moveTo(1.5, 0);
        ctx.lineTo(1.5, HEIGHT);
        ctx.stroke();

        ctx.beginPath();
        ctx.moveTo(WIDTH - 1.5, 0);
        ctx.lineTo(WIDTH - 1.5, HEIGHT);
        ctx.stroke();
        ctx.restore();


        // ----------------------------------------------------
        // CENTER LINE
        // ----------------------------------------------------

        ctx.strokeStyle =
            "rgba(255,255,255,0.15)";


        ctx.lineWidth = 2;


        ctx.setLineDash([
            12,
            14
        ]);


        ctx.beginPath();


        ctx.moveTo(
            WIDTH / 2,
            0
        );


        ctx.lineTo(
            WIDTH / 2,
            HEIGHT
        );


        ctx.stroke();


        ctx.setLineDash([]);



        // ----------------------------------------------------
        // PADDLES
        // ----------------------------------------------------

        ctx.fillStyle =
            "white";


        // Rotate the server's horizontal world into a vertical court.
        // X becomes screen Y; Y becomes screen X. Every dimension is
        // scaled into screen pixels: paddle position/height move along
        // the world-vertical axis (scaleX), paddle thickness moves along
        // the world-horizontal axis (scaleY). Paddles therefore become
        // horizontal, each own paddle at the bottom of its own screen
        // (see perpToScreen above).

        const paddleThicknessPx =
            paddleSize.width * scaleY;

        // Player 1
        ctx.fillRect(
            p1.y * scaleX,

            perpToScreen(p1.x, paddleThicknessPx),

            p1.height * scaleX,

            paddleThicknessPx

        );

        // Player 2
        ctx.fillRect(
            p2.y * scaleX,

            perpToScreen(p2.x, paddleThicknessPx),

            p2.height * scaleX,

            paddleThicknessPx

        );



        // ----------------------------------------------------
        // BALL
        // ----------------------------------------------------
        // Keep the ball visually round even when the canvas is scaled
        // differently on X and Y. Use one screen-space radius based on
        // the smaller scale so the ball remains a true circle.

        const ballRadius =
            (ball.size * Math.min(scaleX, scaleY)) / 2;

        const ballCenterX =
            ball.y * scaleX + ballRadius;

        const ballCenterY =
            perpToScreen(ball.x, ball.size * scaleY) +
            (ball.size * scaleY) / 2;

        ctx.beginPath();

        ctx.arc(
            ballCenterX,
            ballCenterY,
            ballRadius,
            0,
            Math.PI * 2
        );

        ctx.fill();


        // ----------------------------------------------------
        // POWERUP
        // ----------------------------------------------------

        const powerup = latest.powerup;

        if (powerup) {
            const pRx = (powerup.size * scaleX) / 2;
            const pRy = (powerup.size * scaleY) / 2;
            const pcx = powerup.y * scaleX + pRx;
            const pcy = perpToScreen(powerup.x, powerup.size * scaleY) + pRy;
            const isFast = powerup.type === "fast";

            ctx.beginPath();
            ctx.ellipse(pcx, pcy, pRx, pRy, 0, 0, Math.PI * 2);
            ctx.fillStyle = isFast ? "#ffcc00" : "#33aaff";
            ctx.fill();
            ctx.lineWidth = 2;
            ctx.strokeStyle = "rgba(255,255,255,0.6)";
            ctx.stroke();

            ctx.font = "bold 10px Arial";
            ctx.fillStyle = "#000";
            ctx.textAlign = "center";
            ctx.fillText(isFast ? "FAST" : "SLOW", pcx, pcy + 3);
        }


        // ----------------------------------------------------
        // SMALL PLAYER LABEL
        // ----------------------------------------------------

        ctx.font =
            "12px Arial";


        ctx.fillStyle =
            "rgba(255,255,255,0.3)";


        ctx.textAlign =
            "center";


        ctx.fillText(

            side === "left"
                ? "PLAYER 1 • TOP"
                : "PLAYER 2 • BOTTOM",

            WIDTH / 2,

            side === "left"
                ? HEIGHT - 20
                : 20

        );

    }



    // ========================================================
    // GAME LOOP
    // ========================================================

    let animationFrameId = null;

    function loop() {
        draw();
        animationFrameId = requestAnimationFrame(loop);
    }


    // Also started by the startGameBtn handler (see SETUP FLOW), along
    // with connect() -- both QR codes (the setup screen's and the
    // corner one) are rendered on demand there via renderControllerQR()
    // instead of once, unconditionally, here.

}

