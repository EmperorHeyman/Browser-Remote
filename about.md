# Seamless browser remote

> Control your projector's browser — or your entire PC — from your phone. No alt-tabbing, no extra hardware.

## The Problem

We have **1 PC** connected to **3 monitors and a projector**. My girlfriend watches TV (YouTube, Netflix, etc.) on the projector through a browser while I game on the other monitors. Whenever an ad appeared or she wanted to switch shows, I had to alt-tab out of my game to help her navigate. That got old fast.

## The Solution

A **mobile-first web app** that runs on the same PC and lets anyone on the local network control the projector's browser — or the entire desktop — from their phone. No app install needed — just scan a QR code or open a URL.

### How It Works

```
Phone (browser) ──WiFi/HTTPS──▸ FastAPI Server ──CDP──▸ Brave Browser (on projector)
                                      │
                                      └──ctypes──▸ Windows OS cursor (PC mode)
```

The server talks to Brave via **Chrome DevTools Protocol (CDP)** using Playwright. All input is injected via CDP — the active window focus is never stolen, so gaming is uninterrupted. In **PC mode**, input bypasses the browser entirely and controls the OS cursor via Windows ctypes.

## Why HTTPS / SSL Certificates

The server runs over **HTTPS** with self-signed certificates (`key.pem` + `cert.pem`). This is **required**, not optional:

- **Gyroscope access**: Modern browsers (Chrome, Safari, Firefox) block the `DeviceOrientationEvent` and `DeviceMotionEvent` APIs on insecure origins. Without HTTPS, the gyro "Magic Remote" mode simply won't work — the browser will silently refuse to provide sensor data.
- **Safari specifically**: Safari is the strictest — it won't even prompt for sensor permission unless the page is served over HTTPS. On plain HTTP, the gyro sub-mode is completely dead.
- **WebSocket upgrade**: `wss://` (secure WebSocket) is required for real-time mirror and gyro streaming when served over HTTPS. The frontend auto-detects the protocol and upgrades accordingly.

### Generating Certificates

```bash
# Generate self-signed cert (run once in the project directory)
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=projector-remote"
```

On first connect, your phone will show a certificate warning — accept it once and you're good.

---

## Two Operating Modes

### Browser Mode (default)

Controls the projector's browser via CDP. All clicks, scrolls, and keypresses are injected into the browser DOM — your game's window focus is never touched.

- **Mirror**: live stream of the browser tab via CDP `Page.startScreencast`
- **Click/scroll/hover**: mapped to browser viewport coordinates via Playwright
- **Keyboard**: injected into the active tab (arrow keys, Enter, Escape, media keys)
- **Fake cursor**: a DOM-injected SVG pointer with click ripple animations (trackpad mode)
- **Navigation**: open URLs, switch tabs, go back/forward, reload

### PC Mode

Controls the entire Windows desktop. The mirror shows a real-time screenshot of a selected monitor, and all input goes to the OS cursor.

- **Mirror**: live desktop capture via `mss` (Python screen capture library), streaming as JPEG over WebSocket
- **Click**: moves the OS cursor to the tapped position and sends a left-click via `mouse_event`
- **Scroll**: OS-level `MOUSEEVENTF_WHEEL` / `MOUSEEVENTF_HWHEEL` at the cursor position
- **Gyro**: moves the real OS cursor, not a DOM element
- **Monitor switching**: pick which display to mirror and control (each monitor is captured individually)

Toggle between modes with the **🖥️ Browser / ⚠️ PC Mode** button in the top bar.

---

## Features (Phone UI)

The phone interface has five main tabs: **Mirror**, **Remote**, **Keys**, **Browse**, and **Settings** (⚙).

### Mirror Tab

Live view of what's on screen. Primary way to see and interact with content.

| Feature | Description |
|---|---|
| **Live mirroring** | WebSocket stream — CDP screencast in Browser mode, mss desktop capture in PC mode |
| **Tap-to-click** | Tap anywhere on the mirrored image to click that exact position |
| **Swipe to scroll** | Swipe up/down to scroll the page at the touch position |
| **Pinch-to-zoom** | 1x–5x zoom to read small text or target tiny UI elements |
| **Zoom badge** | Shows current zoom level (top-right corner) |
| **Pan** | When zoomed in, drag to pan around the page |
| **Reset zoom** | One-tap button to snap back to 1x |
| **Quick controls** | Esc, Backspace, Play/Pause, Fullscreen, Mute, Back, Forward |
| **Monitor picker** | (PC mode only) Switch which display to mirror and control |
| **Fallback polling** | If WebSocket drops, falls back to screenshot polling until reconnected |

### Remote Tab

Four sub-modes for different control styles:

#### D-Pad
Classic directional pad — up/down/left/right arrows + OK (Enter). Below it:
- Media buttons: Play/Pause, Fullscreen, Mute
- Text input field for typing search queries
- Esc, Backspace shortcuts

#### Combo
Split screen — trackpad on the top half, D-pad + media controls on the bottom. Best of both worlds.

#### Trackpad
Full-screen relative trackpad:
- **Drag to move** a DOM-injected fake cursor (Browser mode) or the OS cursor (PC mode)
- **Tap to click** at the current cursor position (with ripple animation in Browser mode)
- **Velocity-based pointer acceleration** — slow movements are precise (1.0x), fast flicks scale up to 3.5x. Covers a full 1080p screen in one swipe.
- Media and shortcut buttons below the trackpad

#### Gyro ("Magic Remote")
Point your phone at the screen like a Wii Remote or LG Magic Remote:
- **Tap to wake** — first tap activates gyro tracking, your phone tilt starts moving the cursor
- **Tap to click** — while aiming, tap the trigger button to click at the cursor position
- **Auto-sleep** — after 4 seconds of inactivity, gyro goes back to sleep to save battery; tap to wake again
- **DeviceOrientationEvent API** — reads pitch (beta) and yaw (alpha) from the phone's gyroscope
- **EMA low-pass filter** (α = 0.6) — smooths out hand tremor and sensor jitter
- **Asymmetric sensitivity** — horizontal (40 px/°) and vertical (25 px/°) tuned for natural wrist mechanics
- **WebSocket transport** — low-latency cursor streaming (not HTTP polling)
- **Portrait orientation lock** — prevents auto-rotate from scrambling sensor axes mid-use
- **Haptic boundary feedback** — phone vibrates when cursor hits screen edges
- **Boundary clamping** — prevents cursor from going out of bounds
- **Fullscreen prompt** — goes fullscreen on first tap for immersive use
- Works in both Browser mode (DOM cursor) and PC mode (real OS cursor)

### Keys Tab

Full virtual keyboard and media controls for typing and controlling media on the PC:

| Feature | Description |
|---|---|
| **Text input** | Type a string and send it to the PC in one shot |
| **QWERTY keyboard** | Dynamic on-screen keyboard with all alphanumeric keys |
| **Modifiers** | Shift, Ctrl, Alt toggles — combine with any key for shortcuts (e.g. Ctrl+C, Alt+F4) |
| **Special keys** | Tab, Escape, Win, Arrow keys (←↓↑→), Delete, Backspace, Enter |
| **Media controls** | System-level ⏮ Previous, ⏯ Play/Pause, ⏭ Next, 🔇 Mute, 🔉🔊 Volume — works across all apps, not just the browser |
| **Volume slider** | Per-app browser audio control via pycaw, with automatic fallback to system-wide volume (doesn't affect game audio when browser session is available) |
| **PC mode support** | In PC mode, keys are injected via `SendInput` (UNICODE + VK codes) at the OS level |
| **Browser mode support** | In browser mode, uses Playwright `page.keyboard.press()` for DOM-level input |

### Browse Tab

Quick access to content without needing the mirror:
- **Quick Sites** — one-tap cards for YouTube TV, Netflix, Oneplay (configurable in `config.py`)
- **URL bar** — navigate to any URL manually
- **Cast from phone** — paste any URL into the Cast field to push it to the projector browser
- **Favorites** — save frequently visited sites as bookmarks, one-tap to open, delete with ×
- **Tab Manager** — lists all open browser tabs, tap to switch between them
- Active site is highlighted

### Settings Tab (⚙)

Client-side preferences stored in localStorage — persists per device, no server round-trip:

| Setting | Description |
|---|---|
| **Show/hide tabs** | Toggle visibility of Mirror, Remote, Keys, and Browse tabs individually |
| **Default tab on open** | Which tab to show when the page loads (Mirror, Remote, Keys, or Browse) |
| **Hardware volume keys** | Intercept phone volume buttons to control PC volume instead of phone volume |
| **Volume step size** | How much each volume button press changes (2%, 5%, 10%, or 15%) |
| **Haptic feedback** | Enable/disable vibration on all button presses and interactions |

The hardware volume key interception creates a near-silent AudioContext to capture volume key events from the phone's physical buttons, forwarding them as API calls to adjust PC volume in real-time.

### Top Bar

Always visible at the top of the screen:
- **Status dot** — green when connected, red when disconnected
- **Page title** — shows the active tab's title
- **Mode toggle** — switch between Browser and PC mode
- **Connect** — force reconnect to the browser CDP
- **Reload** — reload the active browser tab

### Standby Overlay

When no browser is running on the server:
- Full-screen overlay with a **"Tap to Start"** button
- Remotely launches the browser from the phone — no need to touch the PC

### General UX

| Feature | Description |
|---|---|
| **Haptic feedback** | Vibration on every button, tap, mode switch, gyro trigger, and screen edge hit (can be disabled in Settings) |
| **Toast notifications** | Brief popups for actions ("Connected!", "Switched to Display 2", etc.) |
| **Auto-reconnect** | Exponential backoff WebSocket reconnection. Phone can sleep and auto-resume |
| **Dark theme** | Optimized for use in a dark room next to a projector |
| **No install** | Pure web — works in any mobile browser (Safari, Chrome, Firefox) |

---

## Desktop Launcher (PyQt6)

A native Windows GUI to configure and run everything from a single window.

### Setup Page (scrollable)
- **Browser selector** — auto-detects Brave, Chrome, Edge installations
- **Browser path** — manual override for the executable path
- **Profile picker** — use the browser's real profile (keeps logins/cookies) or an isolated one
- **Default URL** — what opens when the browser launches (default: YouTube TV)
- **CDP port** — Chrome DevTools Protocol port (default: 9222)
- **Server port** — FastAPI server port (default: 5000)
- **Browser Display** — which monitor to fullscreen the browser on (auto-detects all connected displays)
- **Monitor identification** — overlays numbered labels on each display for 3 seconds
- **Options** — Server only (no browser), Start with Windows, Minimize to tray
- **Start button** — launches browser + server in one click

### Advanced Settings (collapsible)
- **Cursor hide delay** — 0.5s–10s, how long before the cursor auto-hides on the projector (default: 2s)
- **Cursor sensitivity** — 0.1x–5.0x multiplier for gyro/trackpad cursor speed
- **Scroll speed** — 0.1x–5.0x multiplier for scroll input
- **Startup URLs** — list of URLs to auto-open as tabs when the browser launches
- **Language selector** — English / Čeština (i18n system with translations dict)
- **Config import/export** — backup/restore settings to a JSON file

### Active Page
Shown after the server starts:
- **QR code** — scan with your phone to connect instantly (auto-installs `qrcode` package if missing)
- **Connection URL** — displayed as text too, in case QR scanning isn't available
- **Connection dashboard** — live display of browser title, server uptime, active clients, and current mode
- **Status indicator** — "Starting...", "Running | https://192.168.x.x:5000", or error state
- **Stop button** — kills the server
- **Collapsible log drawer** — real-time server output, toggle-able to save screen space
- **Log export** — save logs to a text file for debugging

### First-Run Wizard
On first launch (no config file exists), a guided dialog walks new users through:
1. Select browser
2. Pick monitor
3. Start server
4. Scan QR code
5. Explore advanced settings

### System Tray
- Minimizes to tray instead of closing
- Right-click menu: Show, **Quick Launch** (sites + favorites submenu), Restart Browser, Quit
- Tray icon shows connection URL as tooltip

### Other
- Settings persist between sessions via `%APPDATA%/ProjectorRemote/config.json`
- SSL auto-detection — if `key.pem` + `cert.pem` exist, QR/URL/status all show `https://`
- **PyInstaller build**: `pyinstaller projector_remote.spec` to create a standalone `.exe`

---

## Server API Reference

### REST Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Serve the phone UI (index.html) |
| `GET` | `/api/status` | Connection state, viewport, PC mode, monitors |
| `POST` | `/api/connect` | Force reconnect to browser CDP |
| `POST` | `/api/start` | Launch the browser process remotely |
| `POST` | `/api/mode` | Toggle Browser/PC mode |
| `GET` | `/api/mode` | Get current mode |
| `GET` | `/api/monitors` | List all detected displays |
| `POST` | `/api/pc-monitor` | Select active monitor for PC mode |
| `GET` | `/api/pc-monitor` | Get active monitor info |
| `GET` | `/api/tabs` | List all open browser tabs |
| `POST` | `/api/tabs/{index}` | Switch to a specific tab |
| `POST` | `/api/click` | Click at relative (0-1) coordinates |
| `POST` | `/api/dblclick` | Double-click at relative coordinates |
| `POST` | `/api/hover` | Move mouse to relative coordinates |
| `POST` | `/api/scroll` | Scroll at a position with deltaX/deltaY |
| `POST` | `/api/cursor/show` | Inject/show the fake cursor |
| `POST` | `/api/cursor/hide` | Remove the fake cursor |
| `POST` | `/api/cursor/move` | Move cursor by dx/dy delta |
| `POST` | `/api/cursor/set` | Set cursor to absolute position |
| `POST` | `/api/cursor/click` | Click at current cursor position |
| `POST` | `/api/cursor/scroll` | Scroll at current cursor position |
| `GET` | `/api/cursor/position` | Get cursor position |
| `GET` | `/api/screenshot.jpg` | Grab a JPEG screenshot (fallback) |
| `POST` | `/api/press/{key}` | Press a keyboard key |
| `POST` | `/api/type` | Type a text string |
| `POST` | `/api/navigate` | Navigate to a URL (reuses matching tabs) |
| `GET` | `/api/sites` | Get configured quick-launch sites |
| `POST` | `/api/go-back` | Browser back |
| `POST` | `/api/go-forward` | Browser forward |
| `POST` | `/api/reload` | Reload the active tab |
| `GET` | `/api/settings` | Get client settings (cursor delay, sensitivity, scroll speed) |
| `POST` | `/api/settings` | Update client settings |
| `GET` | `/api/favorites` | List saved favorite sites |
| `POST` | `/api/favorites` | Add a favorite (name + URL) |
| `DELETE` | `/api/favorites` | Remove a favorite by URL |
| `GET` | `/api/volume` | Get browser audio volume (pycaw per-session) |
| `POST` | `/api/volume` | Set browser audio volume (0.0–1.0) |
| `GET` | `/api/audio-outputs` | List audio render devices |
| `POST` | `/api/media/{action}` | Media key: play_pause, next, prev, vol_up, vol_down, mute |
| `POST` | `/api/keyboard` | Send keyboard input — text string or key combos with modifiers |
| `GET` | `/api/dashboard` | Server uptime, browser title/URL, active clients, mode, monitors |

### WebSocket Endpoints

| Path | Description |
|---|---|
| `/ws/screencast` | CDP-based browser tab streaming (Browser mode) |
| `/ws/screencast-desktop` | mss-based desktop capture streaming (PC mode) |
| `/ws/gyro` | Bidirectional gyro cursor — receives `{dx, dy}` deltas, sends back `{x, y}` position and `{clamped: true}` when cursor hits screen edge |

---

## Known Problems

| Issue | Details |
|---|---|
| **Switching monitors in PC mode is rough** | When switching the active monitor, the mirror WebSocket disconnects and reconnects. There can be a brief black frame or stutter during the transition. The monitor picker UI works but the switchover isn't seamless. |
| **Gyro scrolling is limited** | The gyro "Magic Remote" mode now has on-screen scroll buttons, but there's no gesture-based scroll. You can also switch to trackpad (two-finger scroll) for continuous scrolling. |
| **Self-signed cert warning** | On first connect, phones show a "Not Secure" warning for the self-signed SSL certificate. You have to manually accept it once. Some browsers (especially Safari) may require navigating to the URL directly and accepting the cert before WebSocket connections work. |
| **Single PC only** | The server uses `ctypes.windll` for mouse control and `mss` for desktop capture — Windows only. No Linux/macOS support. |
| **Volume control requires pycaw** | Browser volume control uses pycaw (Windows Audio Session API). Only works on Windows. Prefers the browser's per-app audio session; falls back to system-wide volume if no browser session is active. All COM calls run in worker threads with explicit `CoInitialize`/`CoUninitialize` for asyncio compatibility. |

---

## Setup

### Prerequisites
- **Windows 10/11** (monitor detection and cursor control use Win32 API)
- **Python 3.10+**
- **Brave**, **Chrome**, or **Edge** browser
- Phone on the **same WiFi network**
- **OpenSSL** (to generate certificates, if not already done)

### Install

```bash
cd RemoteProjector
pip install -r requirements.txt
python -m playwright install chromium
```

### Generate SSL Certificates

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=projector-remote"
```

### Run (GUI Launcher)

```bash
python launcher.py
```

Select your browser, monitor, and click **Start**. The launcher will:
1. Open the browser in fullscreen on your chosen monitor
2. Start the HTTPS server with SSL
3. Show a QR code to scan with your phone

### Run (Command Line)

```powershell
# Option 1: PowerShell script
.\start.ps1

# Option 2: Batch file
start.bat

# Option 3: Manual
# 1. Start browser with CDP
"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" `
    --remote-debugging-port=9222 `
    --user-data-dir="%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data" `
    --start-fullscreen `
    --window-position=6400,134 `
    https://www.youtube.com/tv

# 2. Start server (with SSL)
python -m uvicorn server:app --host 0.0.0.0 --port 5000 --ssl-keyfile key.pem --ssl-certfile cert.pem
```

### Connect

Scan the QR code shown in the launcher, or open `https://<your-pc-ip>:5000` on your phone. Accept the certificate warning on first visit.

---

## Project Structure

```
RemoteProjector/
├── launcher.py             # PyQt6 desktop GUI launcher
├── server.py               # FastAPI backend (CDP + OS cursor bridge)
├── config.py               # Configuration (%APPDATA% persistence)
├── projector_remote.spec   # PyInstaller build spec
├── key.pem                 # SSL private key (self-signed)
├── cert.pem                # SSL certificate (self-signed)
├── static/
│   └── index.html          # Mobile-first web frontend (single file, ~2200 lines)
├── start.ps1               # PowerShell one-click launcher
├── start.bat               # Batch one-click launcher
├── requirements.txt        # Python dependencies
├── idea.md                 # Development notes & changelog
└── about.md                # This file
```

## Configuration

Settings are stored in `%APPDATA%/ProjectorRemote/config.json` and managed via the GUI launcher:

| Setting | Default | Description |
|---|---|---|
| `brave_path` | Auto-detected | Path to browser executable |
| `cdp_port` | `9222` | Chrome DevTools Protocol port |
| `port` | `5000` | HTTPS server port |
| `user_data_dir` | Browser's AppData | Profile directory (keeps logins) |
| `projector_monitor` | `0` | Which display to fullscreen the browser on |
| `default_url` | `https://www.youtube.com/tv` | URL opened when browser launches |
| `sites` | YouTube, Netflix, Oneplay | Quick-launch site shortcuts |
| `cursor_hide_delay` | `2.0` | Seconds before auto-hiding cursor on projector |
| `cursor_sensitivity` | `1.0` | Multiplier for gyro/trackpad cursor speed (0.1–5.0) |
| `scroll_speed` | `1.0` | Multiplier for scroll input (0.1–5.0) |
| `startup_urls` | `[]` | URLs to auto-open as tabs on browser launch |
| `favorites` | `[]` | User-saved favorite sites (name + URL pairs) |
| `language` | `"en"` | UI language for launcher (`en` or `cs`) |

## Tech Stack

- **Backend**: Python 3.10+, FastAPI, Playwright (CDP), uvicorn, SSL/TLS
- **Frontend**: Vanilla HTML/CSS/JS (single file, no build step, no dependencies)
- **Browser Streaming**: WebSocket + CDP `Page.startScreencast` (auto-reconnect with exponential backoff)
- **Desktop Streaming**: `mss` (multi-monitor screenshots) + Pillow (JPEG encoding)
- **Gyroscope**: DeviceOrientationEvent API → WebSocket → server-side cursor accumulation (edge haptic feedback)
- **OS Input**: Windows `ctypes` — `SetCursorPos`, `mouse_event`, `GetCursorPos`, `EnumDisplayMonitors`, `SendInput` (keyboard)
- **Audio Control**: `pycaw` (Windows Audio Session API) for per-app browser volume control
- **Desktop UI**: PyQt6 (stacked widget, system tray, QR code generation, i18n)
- **Packaging**: PyInstaller

## License

MIT
