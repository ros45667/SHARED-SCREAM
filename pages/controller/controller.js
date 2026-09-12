        // ==========================================
        // SETUP
        // ==========================================
        const params = new URLSearchParams(window.location.search);
        const player = params.get("player") === "2" ? 2 : 1;
        document.querySelectorAll("#titleSelect, #titleButtons, #titleMotion").forEach(el => {
            el.textContent = `Controller (player ${player})`;
        });

        const statusEl = document.getElementById("status");
        const bodyEl = document.getElementById("body");
        let ws;

        function connect() {
            const wsScheme = location.protocol === "https:" ? "wss" : "ws";
            ws = new WebSocket(`${wsScheme}://${location.host}/ws/controller-${player}`);
            ws.onopen = () => { statusEl.textContent = "connected"; };
            ws.onclose = () => { statusEl.textContent = "disconnected — retrying..."; setTimeout(connect, 1000); };
            ws.onerror = () => ws.close();
        }
        connect();

        function sendMsg(obj) {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify(Object.assign({ player }, obj)));
            }
        }

        // ==========================================
        // SCREEN SWITCHING
        // ==========================================
        const screens = {
            select: document.getElementById("screen-select"),
            buttons: document.getElementById("screen-buttons"),
            motion: document.getElementById("screen-motion"),
        };
        function showScreen(name) {
            Object.values(screens).forEach(s => s.classList.remove("active"));
            screens[name].classList.add("active");
        }
        document.querySelectorAll("[data-back]").forEach(btn => {
            btn.addEventListener("click", () => {
                stopAllHolds();
                showScreen("select");
            });
        });

        // ==========================================
        // BUTTON MODE
        // ==========================================
        document.getElementById("pickButtons").addEventListener("click", () => showScreen("buttons"));

        // Holding UP/DOWN moves continuously: fire once immediately (so a quick
        // tap still registers), then keep resending at a fixed rate for as long
        // as the button/key stays down. One interval per action so holding both
        // at once (or a key + a button) doesn't clobber each other.
        const HOLD_REPEAT_MS = 1000 / 30;
        const holdIntervals = {};

        function startHold(action) {
            if (holdIntervals[action]) return; // already repeating
            sendMsg({ action });
            holdIntervals[action] = setInterval(() => sendMsg({ action }), HOLD_REPEAT_MS);
        }
        function stopHold(action) {
            if (holdIntervals[action]) {
                clearInterval(holdIntervals[action]);
                delete holdIntervals[action];
            }
        }
        function stopAllHolds() {
            Object.keys(holdIntervals).forEach(stopHold);
        }

        function bindHoldButton(btn, action) {
            btn.addEventListener("pointerdown", (e) => {
                e.preventDefault();
                btn.setPointerCapture(e.pointerId);
                startHold(action);
            });
            btn.addEventListener("pointerup", () => stopHold(action));
            btn.addEventListener("pointercancel", () => stopHold(action));
        }
        bindHoldButton(document.getElementById("up"), "up");
        bindHoldButton(document.getElementById("down"), "down");

        window.addEventListener("keydown", (e) => {
            if (!screens.buttons.classList.contains("active")) return;
            if (e.key === "ArrowUp" || e.key === "w" || e.key === "W") startHold("up");
            if (e.key === "ArrowDown" || e.key === "s" || e.key === "S") startHold("down");
        });
        window.addEventListener("keyup", (e) => {
            if (e.key === "ArrowUp" || e.key === "w" || e.key === "W") stopHold("up");
            if (e.key === "ArrowDown" || e.key === "s" || e.key === "S") stopHold("down");
        });

        // Don't leave a paddle stuck moving if focus/visibility is lost mid-hold,
        // or if the user backs out of the button screen while still pressing.
        window.addEventListener("blur", stopAllHolds);
        document.addEventListener("visibilitychange", () => {
            if (document.hidden) stopAllHolds();
        });

        // ==========================================
        // MOTION MODE
        // ==========================================
        document.getElementById("pickMotion").addEventListener("click", () => {
            showScreen("motion");
            if (!window.isSecureContext) {
                document.getElementById("secureWarning").style.display = "block";
            }
        });

        const TILT_RANGE_DEG = 40;     // full tilt range mapped to top..bottom of paddle
        const HIT_JERK_THRESHOLD = 9;  // m/s^2 sudden spike needed to count as a "swing"
        const HIT_JERK_MAX = 28;       // spike magnitude that counts as a full-power (1.0) swing
        const HIT_COOLDOWN_MS = 250;   // minimum time between two swings
        const ORIENTATION_SEND_INTERVAL_MS = 1000 / 20; // throttle position updates to 20Hz

        let baselineBeta = null;
        let latestBeta = null;
        let lastOrientationSend = 0;

        let smoothedMag = null;
        let lastHitTime = 0;

        const tiltPuck = document.getElementById("tiltPuck");
        const tiltTrack = document.getElementById("tiltTrack");
        const swingFill = document.getElementById("swingMeterFill");

        function updateTiltUI(normalized) {
            const trackHeight = tiltTrack.clientHeight - tiltPuck.clientHeight;
            tiltPuck.style.top = `${normalized * trackHeight}px`;
        }

        function handleOrientation(event) {
            if (event.beta === null || event.beta === undefined) return;
            latestBeta = event.beta;
            if (baselineBeta === null) baselineBeta = event.beta;

            const now = performance.now();
            if (now - lastOrientationSend < ORIENTATION_SEND_INTERVAL_MS) return;
            lastOrientationSend = now;

            // event.beta wraps around at +-180 degrees. A plain subtraction
            // breaks near that wrap point: e.g. baseline=170, beta=-170 is
            // only a 20-degree tilt in reality, but 170 - (-170) = 340
            // divided by TILT_RANGE_DEG instantly maxes out the clamp --
            // which is exactly what shows up as the paddle snapping all
            // the way from one edge to the other on an ordinary small
            // tilt. Wrapping the difference into (-180, 180] first gives
            // the true shortest angular distance instead.
            let delta = event.beta - baselineBeta; // tilt forward (away from body) = negative-going in most holds
            delta = ((delta + 180) % 360 + 360) % 360 - 180;
            let normalized = 0.5 - (delta / TILT_RANGE_DEG) * 0.5;
            normalized = Math.max(0, Math.min(1, normalized));

            updateTiltUI(normalized);
            sendMsg({ action: "set", value: normalized });
        }

        function flashHit() {
            tiltPuck.classList.add("flash");
            bodyEl.style.backgroundColor = "#221a00";
            setTimeout(() => {
                tiltPuck.classList.remove("flash");
                bodyEl.style.backgroundColor = "#000";
            }, 180);
            if (navigator.vibrate) navigator.vibrate(40);
        }

        function handleMotion(event) {
            const raw = (event.acceleration && event.acceleration.x !== null)
                ? event.acceleration
                : event.accelerationIncludingGravity;
            if (!raw || raw.x === null || raw.x === undefined) return;

            const mag = Math.sqrt((raw.x || 0) ** 2 + (raw.y || 0) ** 2 + (raw.z || 0) ** 2);
            if (smoothedMag === null) smoothedMag = mag;

            const jerk = mag - smoothedMag; // sudden spike above the slow-moving baseline = a swing
            smoothedMag = smoothedMag * 0.9 + mag * 0.1;

            swingFill.style.width = `${Math.min(100, Math.max(0, (jerk / HIT_JERK_MAX) * 100))}%`;

            const now = performance.now();
            if (jerk > HIT_JERK_THRESHOLD && now - lastHitTime > HIT_COOLDOWN_MS) {
                lastHitTime = now;
                const power = Math.min(1, jerk / HIT_JERK_MAX);
                sendMsg({ action: "hit", power });
                flashHit();
            }
        }

        async function enableMotion() {
            try {
                if (typeof DeviceMotionEvent !== "undefined" && typeof DeviceMotionEvent.requestPermission === "function") {
                    const perm = await DeviceMotionEvent.requestPermission();
                    if (perm !== "granted") { alert("Motion permission denied."); return; }
                }
                if (typeof DeviceOrientationEvent !== "undefined" && typeof DeviceOrientationEvent.requestPermission === "function") {
                    const perm = await DeviceOrientationEvent.requestPermission();
                    if (perm !== "granted") { alert("Orientation permission denied."); return; }
                }
            } catch (err) {
                alert("Couldn't get sensor permission: " + err);
                return;
            }

            window.addEventListener("deviceorientation", handleOrientation);
            window.addEventListener("devicemotion", handleMotion);

            document.getElementById("motionPrompt").style.display = "none";
            document.getElementById("motionActive").style.display = "block";
        }

        document.getElementById("enableMotion").addEventListener("click", enableMotion);
        document.getElementById("recalibrate").addEventListener("click", () => {
            if (latestBeta !== null) baselineBeta = latestBeta;
        });

        // ==========================================
        // SCREAM POWER (mic-based, independent of button/motion mode)
        // ==========================================
        // Tunable calibration: getByteTimeDomainData gives bytes centered on
        // 128, so RMS deviation is roughly 0 in a silent room and can climb
        // past 0.4 for a real scream close to the mic. Adjust these if it
        // feels too twitchy (raise SCREAM_MIN_RMS) or too hard to trigger
        // (lower SCREAM_MAX_RMS).
        const SCREAM_MIN_RMS = 0.03;   // quieter than this counts as 0
        const SCREAM_MAX_RMS = 0.35;   // this loud (or louder) counts as full 1.0
        const SCREAM_SEND_INTERVAL_MS = 1000 / 20; // throttle to 20Hz, same as tilt
        const SCREAM_FLASH_THRESHOLD = 0.85;       // screen flashes above this level

        let screamEnabled = false;
        let audioCtx, analyser, screamDataArray;
        let lastScreamSend = 0;
        let lastScreamFlash = 0;

        const screamPrompts = [
            document.getElementById("screamPromptButtons"),
            document.getElementById("screamPromptMotion"),
        ];
        const screamMeters = [
            document.getElementById("screamMeterButtons"),
            document.getElementById("screamMeterMotion"),
        ];

        function setScreamMeterFill(level) {
            screamMeters.forEach(el => {
                const fill = el.querySelector(".scream-meter-fill");
                if (fill) fill.style.width = `${Math.round(level * 100)}%`;
            });
        }

        function screamLoop() {
            if (!screamEnabled) return;
            requestAnimationFrame(screamLoop);

            analyser.getByteTimeDomainData(screamDataArray);
            let sumSquares = 0;
            for (let i = 0; i < screamDataArray.length; i++) {
                const dev = (screamDataArray[i] - 128) / 128;
                sumSquares += dev * dev;
            }
            const rms = Math.sqrt(sumSquares / screamDataArray.length);
            let level = (rms - SCREAM_MIN_RMS) / (SCREAM_MAX_RMS - SCREAM_MIN_RMS);
            level = Math.max(0, Math.min(1, level));

            setScreamMeterFill(level);

            const now = performance.now();
            if (level >= SCREAM_FLASH_THRESHOLD && now - lastScreamFlash > 200) {
                lastScreamFlash = now;
                bodyEl.classList.add("scream-flash");
                setTimeout(() => bodyEl.classList.remove("scream-flash"), 150);
                if (navigator.vibrate) navigator.vibrate(30);
            }

            if (now - lastScreamSend >= SCREAM_SEND_INTERVAL_MS) {
                lastScreamSend = now;
                sendMsg({ action: "scream", level });
            }
        }

        async function enableScream() {
            try {
                const micStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
                audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                const source = audioCtx.createMediaStreamSource(micStream);
                analyser = audioCtx.createAnalyser();
                analyser.fftSize = 512;
                screamDataArray = new Uint8Array(analyser.fftSize);
                source.connect(analyser);
            } catch (err) {
                alert("Couldn't get microphone permission: " + err);
                return;
            }

            screamEnabled = true;
            screamPrompts.forEach(el => { el.style.display = "none"; });
            screamMeters.forEach(el => { el.style.display = "block"; });
            requestAnimationFrame(screamLoop);
        }

        document.querySelectorAll("[data-enable-scream]").forEach(btn => {
            btn.addEventListener("click", enableScream);
        });
