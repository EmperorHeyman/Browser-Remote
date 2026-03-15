"""
Seamless browser remote - FastAPI Backend (v2)
Controls Brave Browser via Chrome DevTools Protocol using Playwright.
All inputs are injected via CDP so the active window focus is never stolen.

Key features:
  - Live mirror mode: stream screenshots to phone, tap-to-click
  - Mouse click/scroll/hover injection via CDP
  - Tab management: reuse existing tabs, don't open duplicates
  - D-Pad keyboard mode as fallback
  - Text input for search
"""

import asyncio
import base64
import ctypes
import ctypes.wintypes
import json
import logging
import os
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, Page, BrowserContext

import config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("remote")

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
pw_instance = None
browser: Optional[Browser] = None
page: Optional[Page] = None
browser_process: Optional[subprocess.Popen] = None
pc_mode = False
pc_monitor = 0  # which monitor index to use in PC mode (0 = primary)


def detect_monitors() -> list[dict]:
    """Detect monitors via EnumDisplayMonitors."""
    monitors = []
    try:
        MONITOR_ENUM_PROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_double,
        )

        def callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
            r = lprcMonitor.contents
            monitors.append({
                "left": r.left, "top": r.top,
                "width": r.right - r.left, "height": r.bottom - r.top,
            })
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(
            None, None, MONITOR_ENUM_PROC(callback), 0
        )
    except Exception:
        pass
    if not monitors:
        w, h = get_screen_size()
        monitors.append({"left": 0, "top": 0, "width": w, "height": h})
    return monitors


def get_active_monitor() -> dict:
    """Return the monitor dict for the currently selected pc_monitor index."""
    monitors = detect_monitors()
    idx = pc_monitor if 0 <= pc_monitor < len(monitors) else 0
    return monitors[idx]


# ---------------------------------------------------------------------------
# OS-level mouse control (Windows ctypes — zero external deps)
# ---------------------------------------------------------------------------
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def os_cursor_move_rel(dx: float, dy: float):
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    ctypes.windll.user32.SetCursorPos(pt.x + int(dx), pt.y + int(dy))


def os_cursor_set(x: float, y: float):
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def os_cursor_click():
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def os_cursor_position():
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def get_screen_size():
    """Get full virtual screen size."""
    w = ctypes.windll.user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    h = ctypes.windll.user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
    if w == 0 or h == 0:
        w = ctypes.windll.user32.GetSystemMetrics(0)  # SM_CXSCREEN
        h = ctypes.windll.user32.GetSystemMetrics(1)  # SM_CYSCREEN
    return w, h

# ---------------------------------------------------------------------------
# Allowed keys
# ---------------------------------------------------------------------------
ALLOWED_KEYS = {
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
    "Enter", "Escape", "Backspace", "Tab",
    "MediaPlayPause", "MediaTrackNext", "MediaTrackPrevious",
    "k", "j", "l", "f", "m", " ",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def get_all_pages() -> list[Page]:
    if browser is None:
        return []
    pages = []
    for ctx in browser.contexts:
        pages.extend(ctx.pages)
    return pages


async def find_target_page() -> Optional[Page]:
    pages = await get_all_pages()
    return pages[0] if pages else None


async def get_viewport():
    vp = page.viewport_size
    if vp and vp.get("width"):
        return vp
    return await page.evaluate("({width: window.innerWidth, height: window.innerHeight})")


async def ensure_connection():
    global pw_instance, browser, page

    if page is not None:
        try:
            await page.title()
            return
        except Exception:
            log.warning("Lost connection, reconnecting...")
            page = None
            browser = None

    if pw_instance is None:
        pw_instance = await async_playwright().start()

    cdp_url = f"http://localhost:{config.CDP_PORT}"
    log.info(f"Connecting to CDP at {cdp_url} ...")
    try:
        browser = await pw_instance.chromium.connect_over_cdp(cdp_url)
    except Exception as e:
        log.error(f"CDP connection failed: {e}")
        raise HTTPException(status_code=503, detail=f"Cannot connect to Brave CDP: {e}")

    page = await find_target_page()
    if page is None:
        raise HTTPException(status_code=503, detail="No browser page found")

    log.info(f"Connected! {await page.title()} | {page.url}")


def launch_browser():
    """Launch browser process from config (server is the boss)."""
    global browser_process
    cfg = config.load()
    browser_path = cfg["brave_path"]
    cdp_port = cfg["cdp_port"]
    default_url = cfg["default_url"]
    user_data_dir = cfg["user_data_dir"]
    monitors = cfg.get("monitors", {})
    proj_mon = str(cfg.get("projector_monitor", 0))

    if not os.path.isfile(browser_path):
        log.error(f"Browser not found: {browser_path}")
        return

    args = [
        browser_path,
        f"--remote-debugging-port={cdp_port}",
        "--start-fullscreen",
        "--no-first-run",
        "--disable-features=TranslateUI",
        "--autoplay-policy=no-user-gesture-required",
    ]
    if user_data_dir and os.path.isdir(user_data_dir):
        args.append(f"--user-data-dir={user_data_dir}")

    mon = monitors.get(proj_mon, monitors.get(int(proj_mon) if proj_mon.isdigit() else -1))
    if mon:
        args.append(f"--window-position={mon['left']},{mon['top']}")

    args.append(default_url)

    try:
        browser_process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS,
        )
        log.info(f"Browser launched (PID {browser_process.pid})")
    except Exception as e:
        log.error(f"Failed to launch browser: {e}")


async def smart_navigate(url: str) -> Page:
    global page
    await ensure_connection()

    target_domain = urlparse(url).netloc.replace("www.", "")

    pages = await get_all_pages()
    for p in pages:
        try:
            pd = urlparse(p.url).netloc.replace("www.", "")
            if target_domain and target_domain in pd:
                page = p
                await page.bring_to_front()
                if p.url.rstrip("/") != url.rstrip("/"):
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                log.info(f"Reused tab for {url}")
                return page
        except Exception:
            continue

    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
    log.info(f"Navigated to {url}")
    return page


def get_local_ip() -> str:
    """Get the real LAN IP by connecting to an external address (no traffic sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------
# HTTP -> HTTPS redirect server
# ---------------------------------------------------------------------------
def _find_ssl_files() -> tuple[str | None, str | None]:
    """Locate key.pem and cert.pem."""
    search_dirs = []
    if getattr(sys, 'frozen', False):
        search_dirs.append(os.path.dirname(sys.executable))
        search_dirs.append(getattr(sys, '_MEIPASS', ''))
    else:
        search_dirs.append(os.path.dirname(os.path.abspath(__file__)))
    for d in search_dirs:
        if not d:
            continue
        key = os.path.join(d, 'key.pem')
        cert = os.path.join(d, 'cert.pem')
        if os.path.isfile(key) and os.path.isfile(cert):
            return key, cert
    return None, None


async def _http_redirect_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle an HTTP request and redirect it to HTTPS."""
    try:
        # Read the request line
        request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not request_line:
            writer.close()
            return

        # Parse the request to get the path
        parts = request_line.decode('utf-8', errors='ignore').split()
        path = parts[1] if len(parts) > 1 else '/'

        # Read and discard headers
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if line in (b'\r\n', b'\n', b''):
                break

        # Build redirect URL
        local_ip = get_local_ip()
        https_port = config.PORT
        redirect_url = f"https://{local_ip}:{https_port}{path}"

        # Send redirect response
        response = (
            f"HTTP/1.1 301 Moved Permanently\r\n"
            f"Location: {redirect_url}\r\n"
            f"Content-Length: 0\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(response.encode('utf-8'))
        await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start_http_redirect_server(http_port: int = 80) -> asyncio.Server | None:
    """Start a simple HTTP server that redirects all requests to HTTPS."""
    try:
        server = await asyncio.start_server(
            _http_redirect_handler,
            host='0.0.0.0',
            port=http_port,
        )
        log.info(f"HTTP redirect server started on port {http_port} -> HTTPS:{config.PORT}")
        return server
    except PermissionError:
        log.warning(f"Cannot bind to port {http_port} (requires admin). HTTP redirect disabled.")
        return None
    except OSError as e:
        if e.errno == 10048:  # Port already in use (Windows)
            log.warning(f"Port {http_port} already in use. HTTP redirect disabled.")
        else:
            log.warning(f"Could not start HTTP redirect server: {e}")
        return None
    except Exception as e:
        log.warning(f"HTTP redirect server failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("=== browser remote v4 starting ===")

    # Check if browser launch should be skipped
    no_browser = (
        os.environ.get("BR_NO_BROWSER", "") == "1"
        or config.load().get("no_browser", False)
    )

    if no_browser:
        log.info("Server-only mode — browser will NOT be launched automatically.")
        # Still try connecting to an already-running browser
        try:
            await ensure_connection()
            log.info("Connected to existing browser")
        except Exception:
            log.info("No browser running — waiting for remote launch via /api/start")
    else:
        # Try connecting to an existing browser
        try:
            await ensure_connection()
            log.info("Connected to existing browser")
        except Exception:
            log.info("No browser found — launching from config...")
            try:
                launch_browser()
                await asyncio.sleep(3)
                await ensure_connection()
                log.info("Browser launched and connected")
                await _open_startup_urls()
            except Exception as e:
                log.warning(f"Browser launch/connect failed: {e} — server in standby")

    local_ip = get_local_ip()
    
    # Start HTTP redirect server if SSL is enabled
    http_redirect_server = None
    key_file, cert_file = _find_ssl_files()
    if key_file:
        # SSL enabled - start HTTP redirect on port 80
        http_redirect_server = await start_http_redirect_server(80)
        log.info(f"  Local:   https://localhost:{config.PORT}")
        log.info(f"  Network: https://{local_ip}:{config.PORT}")
    else:
        log.info(f"  Local:   http://localhost:{config.PORT}")
        log.info(f"  Network: http://{local_ip}:{config.PORT}")
    
    yield

    # Cleanup redirect server
    if http_redirect_server:
        http_redirect_server.close()
        await http_redirect_server.wait_closed()
        log.info("HTTP redirect server stopped")

    global pw_instance, browser, page
    if browser:
        try: await browser.close()
        except: pass
    if pw_instance:
        await pw_instance.stop()
    log.info("=== Stopped ===")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="browser remote", lifespan=lifespan)
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class KeyPress(BaseModel):
    key: str

class TypeText(BaseModel):
    text: str

class Navigate(BaseModel):
    url: str

class ClickAction(BaseModel):
    x: float
    y: float

class ScrollAction(BaseModel):
    x: float
    y: float
    deltaX: float = 0
    deltaY: float = 0

class CursorMove(BaseModel):
    dx: float = 0
    dy: float = 0

class CursorSet(BaseModel):
    x: float
    y: float

class CursorScrollAction(BaseModel):
    deltaX: float = 0
    deltaY: float = 0

class ModeSwitch(BaseModel):
    mode: str  # "browser" or "pc"

class MonitorSwitch(BaseModel):
    monitor: int


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return FileResponse(os.path.join(static_dir, "index.html"))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@app.get("/api/status")
async def status():
    global page
    connected = False
    title = url = ""
    vp_w = vp_h = 0
    browser_alive = browser_process is not None and browser_process.poll() is None
    if page:
        try:
            title = await page.title()
            url = page.url
            vp = await get_viewport()
            vp_w, vp_h = vp["width"], vp["height"]
            connected = True
        except: pass
    monitors = detect_monitors()
    return {"connected": connected, "title": title, "url": url,
            "viewport": {"width": vp_w, "height": vp_h},
            "browser_alive": browser_alive, "pc_mode": pc_mode,
            "pc_monitor": pc_monitor, "monitors": monitors}

@app.post("/api/connect")
async def connect():
    await ensure_connection()
    return {"status": "connected", "title": await page.title() if page else ""}


@app.post("/api/start")
async def start_browser_endpoint():
    """Launch / re-launch the browser (standby → active)."""
    global page, browser
    # Reset stale connection
    page = None
    browser = None
    launch_browser()
    # Wait for browser to init, then connect
    for attempt in range(6):
        await asyncio.sleep(1)
        try:
            await ensure_connection()
            return {"status": "ok", "title": await page.title() if page else ""}
        except Exception:
            pass
    return {"status": "starting", "message": "Browser launching — refresh in a few seconds"}


@app.post("/api/mode")
async def set_mode(data: ModeSwitch):
    """Toggle between Browser mode and PC mode."""
    global pc_mode
    pc_mode = (data.mode == "pc")
    log.info(f"Mode switched to: {'PC' if pc_mode else 'Browser'}")
    return {"mode": "pc" if pc_mode else "browser"}


@app.get("/api/mode")
async def get_mode():
    return {"mode": "pc" if pc_mode else "browser"}


@app.get("/api/monitors")
async def list_monitors():
    """List all detected monitors."""
    monitors = detect_monitors()
    return {"monitors": monitors, "active": pc_monitor}


@app.post("/api/pc-monitor")
async def set_pc_monitor(data: MonitorSwitch):
    """Set the active monitor for PC mode."""
    global pc_monitor
    monitors = detect_monitors()
    if data.monitor < 0 or data.monitor >= len(monitors):
        raise HTTPException(400, f"Monitor {data.monitor} out of range (0-{len(monitors)-1})")
    pc_monitor = data.monitor
    mon = monitors[pc_monitor]
    log.info(f"PC monitor set to {pc_monitor}: {mon['width']}x{mon['height']} at ({mon['left']},{mon['top']})")
    return {"status": "ok", "monitor": pc_monitor, "info": mon}


@app.get("/api/pc-monitor")
async def get_pc_monitor():
    monitors = detect_monitors()
    idx = pc_monitor if 0 <= pc_monitor < len(monitors) else 0
    return {"monitor": idx, "info": monitors[idx]}


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
@app.get("/api/tabs")
async def list_tabs():
    await ensure_connection()
    pages = await get_all_pages()
    tabs = []
    for i, p in enumerate(pages):
        try: tabs.append({"index": i, "title": await p.title(), "url": p.url, "active": p == page})
        except: tabs.append({"index": i, "title": "?", "url": "?", "active": False})
    return {"tabs": tabs}

@app.post("/api/tabs/{index}")
async def switch_tab(index: int):
    global page
    await ensure_connection()
    pages = await get_all_pages()
    if index < 0 or index >= len(pages):
        raise HTTPException(400, f"Tab {index} out of range")
    page = pages[index]
    await page.bring_to_front()
    return {"status": "ok", "title": await page.title(), "url": page.url}


# ---------------------------------------------------------------------------
# Mouse (Mirror mode core)
# ---------------------------------------------------------------------------
@app.post("/api/click")
async def click(data: ClickAction):
    if pc_mode:
        mon = get_active_monitor()
        ax = mon["left"] + data.x * mon["width"]
        ay = mon["top"] + data.y * mon["height"]
        os_cursor_set(ax, ay)
        os_cursor_click()
        log.info(f"PC Click ({ax:.0f}, {ay:.0f}) on monitor {pc_monitor}")
        return {"status": "ok", "x": ax, "y": ay}
    await ensure_connection()
    vp = await get_viewport()
    ax, ay = data.x * vp["width"], data.y * vp["height"]
    await page.mouse.click(ax, ay)
    log.info(f"Click ({ax:.0f}, {ay:.0f})")
    return {"status": "ok", "x": ax, "y": ay}

@app.post("/api/dblclick")
async def dblclick(data: ClickAction):
    await ensure_connection()
    vp = await get_viewport()
    ax, ay = data.x * vp["width"], data.y * vp["height"]
    await page.mouse.dblclick(ax, ay)
    return {"status": "ok"}

@app.post("/api/hover")
async def hover(data: ClickAction):
    await ensure_connection()
    vp = await get_viewport()
    await page.mouse.move(data.x * vp["width"], data.y * vp["height"])
    return {"status": "ok"}

@app.post("/api/scroll")
async def scroll(data: ScrollAction):
    if pc_mode:
        mon = get_active_monitor()
        ax = mon["left"] + data.x * mon["width"]
        ay = mon["top"] + data.y * mon["height"]
        os_cursor_set(ax, ay)
        # Windows mouse_event scroll: MOUSEEVENTF_WHEEL=0x0800, delta in multiples of 120
        MOUSEEVENTF_WHEEL = 0x0800
        MOUSEEVENTF_HWHEEL = 0x01000
        if data.deltaY != 0:
            wheel_delta = int(-data.deltaY)  # deltaY comes as px-like; negate for natural scroll
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, wheel_delta, 0)
        if data.deltaX != 0:
            wheel_delta = int(data.deltaX)
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_HWHEEL, 0, 0, wheel_delta, 0)
        log.info(f"PC Scroll dy={data.deltaY:.0f}")
        return {"status": "ok"}
    await ensure_connection()
    vp = await get_viewport()
    await page.mouse.move(data.x * vp["width"], data.y * vp["height"])
    await page.mouse.wheel(data.deltaX, data.deltaY)
    log.info(f"Scroll dy={data.deltaY:.0f}")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Fake Cursor (Trackpad mode)
# ---------------------------------------------------------------------------
# Server-side cursor position in viewport pixels
cursor_pos = {"x": 0.0, "y": 0.0, "visible": False}

# Cursor auto-hide: hide after configurable delay of no movement, re-show on movement
cursor_last_move_time: float = 0.0
cursor_hide_task: Optional[asyncio.Task] = None

CURSOR_INJECT_JS = """
() => {
    if (document.getElementById('__rc_cursor')) return;

    // Inject ripple keyframes once
    if (!document.getElementById('__rc_styles')) {
        const st = document.createElement('style');
        st.id = '__rc_styles';
        st.textContent = `
            @keyframes __rc_ripple {
                0%   { transform: translate(-50%,-50%) scale(0.3); opacity: 1; }
                100% { transform: translate(-50%,-50%) scale(2.5); opacity: 0; }
            }
            .__rc_click_ring {
                position: fixed; pointer-events: none; z-index: 2147483646;
                width: 40px; height: 40px; border-radius: 50%;
                border: 3px solid rgba(233,69,96,0.9);
                transform: translate(-50%,-50%) scale(0.3);
                animation: __rc_ripple 0.45s ease-out forwards;
            }
        `;
        document.documentElement.appendChild(st);
    }

    const c = document.createElement('div');
    c.id = '__rc_cursor';
    c.style.cssText = `
        position: fixed; z-index: 2147483647;
        width: 32px; height: 32px; pointer-events: none;
        left: 50%; top: 50%;
        filter: drop-shadow(0 2px 4px rgba(0,0,0,0.5));
        will-change: left, top;
    `;
    c.innerHTML = `<svg width="32" height="32" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M4 2L4 20L8.5 15.5L12.5 22L15 20.5L11 14L17 14L4 2Z"
              fill="white" stroke="black" stroke-width="1.2" stroke-linejoin="round"/>
    </svg>`;
    document.documentElement.appendChild(c);
}
"""

CURSOR_UPDATE_JS = """
(pos) => {
    const c = document.getElementById('__rc_cursor');
    if (!c) return false;
    c.style.left = pos.x + 'px';
    c.style.top = pos.y + 'px';
    return true;
}
"""

CURSOR_CLICK_RIPPLE_JS = """
(pos) => {
    const ring = document.createElement('div');
    ring.className = '__rc_click_ring';
    ring.style.left = pos.x + 'px';
    ring.style.top = pos.y + 'px';
    document.documentElement.appendChild(ring);
    setTimeout(() => ring.remove(), 500);
}
"""

CURSOR_REMOVE_JS = """
() => {
    const c = document.getElementById('__rc_cursor');
    if (c) c.remove();
    const st = document.getElementById('__rc_styles');
    if (st) st.remove();
    document.querySelectorAll('.__rc_click_ring').forEach(e => e.remove());
}
"""


@app.post("/api/cursor/show")
async def cursor_show():
    """Inject the fake cursor into the page DOM and center it."""
    global cursor_pos
    if pc_mode:
        cursor_pos["visible"] = True
        x, y = os_cursor_position()
        cursor_pos["x"], cursor_pos["y"] = float(x), float(y)
        return {"status": "ok", "x": cursor_pos["x"], "y": cursor_pos["y"]}
    await ensure_connection()
    vp = await get_viewport()
    cursor_pos = {"x": vp["width"] / 2, "y": vp["height"] / 2, "visible": True}
    await page.evaluate(CURSOR_INJECT_JS)
    await page.evaluate(CURSOR_UPDATE_JS, {"x": cursor_pos["x"], "y": cursor_pos["y"]})
    log.info("Cursor shown")
    return {"status": "ok", "x": cursor_pos["x"], "y": cursor_pos["y"]}


@app.post("/api/cursor/hide")
async def cursor_hide():
    """Remove the fake cursor from the DOM."""
    global cursor_pos
    cursor_pos["visible"] = False
    if not pc_mode:
        await ensure_connection()
        await page.evaluate(CURSOR_REMOVE_JS)
    log.info("Cursor hidden")
    return {"status": "ok"}


@app.post("/api/cursor/move")
async def cursor_move(data: CursorMove):
    """Move cursor by relative delta."""
    global cursor_pos
    if pc_mode:
        os_cursor_move_rel(data.dx, data.dy)
        x, y = os_cursor_position()
        cursor_pos["x"], cursor_pos["y"] = float(x), float(y)
        return {"status": "ok", "x": cursor_pos["x"], "y": cursor_pos["y"]}
    await ensure_connection()
    vp = await get_viewport()
    cursor_pos["x"] = max(0, min(vp["width"], cursor_pos["x"] + data.dx))
    cursor_pos["y"] = max(0, min(vp["height"], cursor_pos["y"] + data.dy))
    await page.evaluate(CURSOR_UPDATE_JS, {"x": cursor_pos["x"], "y": cursor_pos["y"]})
    return {"status": "ok", "x": cursor_pos["x"], "y": cursor_pos["y"]}


@app.post("/api/cursor/set")
async def cursor_set(data: CursorSet):
    """Set cursor to absolute position."""
    global cursor_pos
    if pc_mode:
        os_cursor_set(data.x, data.y)
        cursor_pos["x"], cursor_pos["y"] = data.x, data.y
        return {"status": "ok", "x": cursor_pos["x"], "y": cursor_pos["y"]}
    await ensure_connection()
    vp = await get_viewport()
    cursor_pos["x"] = max(0, min(vp["width"], data.x))
    cursor_pos["y"] = max(0, min(vp["height"], data.y))
    await page.evaluate(CURSOR_UPDATE_JS, {"x": cursor_pos["x"], "y": cursor_pos["y"]})
    return {"status": "ok", "x": cursor_pos["x"], "y": cursor_pos["y"]}


@app.post("/api/cursor/click")
async def cursor_click():
    """Click at the current cursor position with visual ripple."""
    global cursor_pos
    if pc_mode:
        os_cursor_click()
        x, y = os_cursor_position()
        cursor_pos["x"], cursor_pos["y"] = float(x), float(y)
        log.info(f"PC cursor click ({x}, {y})")
        return {"status": "ok", "x": cursor_pos["x"], "y": cursor_pos["y"]}
    await ensure_connection()
    if not cursor_pos["visible"]:
        raise HTTPException(400, "Cursor not visible")
    x, y = cursor_pos["x"], cursor_pos["y"]
    await page.evaluate(CURSOR_CLICK_RIPPLE_JS, {"x": x, "y": y})
    await page.mouse.click(x, y)
    log.info(f"Cursor click ({x:.0f}, {y:.0f})")
    return {"status": "ok", "x": x, "y": y}


@app.post("/api/cursor/scroll")
async def cursor_scroll(data: CursorScrollAction):
    """Scroll at the current cursor position."""
    global cursor_pos
    if pc_mode:
        MOUSEEVENTF_WHEEL = 0x0800
        MOUSEEVENTF_HWHEEL = 0x01000
        if data.deltaY != 0:
            ctypes.windll.user32.mouse_event(
                MOUSEEVENTF_WHEEL, 0, 0, int(-data.deltaY), 0)
        if data.deltaX != 0:
            ctypes.windll.user32.mouse_event(
                MOUSEEVENTF_HWHEEL, 0, 0, int(data.deltaX), 0)
        log.info(f"PC cursor scroll dy={data.deltaY:.0f}")
        return {"status": "ok"}
    await ensure_connection()
    await page.mouse.move(cursor_pos["x"], cursor_pos["y"])
    await page.mouse.wheel(data.deltaX, data.deltaY)
    log.info(f"Cursor scroll dy={data.deltaY:.0f}")
    return {"status": "ok"}


@app.get("/api/cursor/position")
async def cursor_position():
    """Get current cursor position."""
    return cursor_pos


# ---------------------------------------------------------------------------
# Screenshot (fallback)
# ---------------------------------------------------------------------------
@app.get("/api/screenshot.jpg")
async def screenshot_jpg(quality: int = 35):
    await ensure_connection()
    q = max(10, min(quality, 90))
    img = await page.screenshot(type="jpeg", quality=q)
    return StreamingResponse(
        iter([img]), media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store"},
    )


# ---------------------------------------------------------------------------
# WebSocket Screencast (CDP Page.screencastFrame)
# ---------------------------------------------------------------------------
@app.websocket("/ws/screencast")
async def ws_screencast(websocket: WebSocket):
    """
    Stream live frames via CDP Page.startScreencast.
    Much more efficient than polling screenshots — browser only encodes
    frames when the page actually changes.
    """
    await websocket.accept()
    await ensure_connection()

    cdp = await page.context.new_cdp_session(page)

    frame_queue: asyncio.Queue = asyncio.Queue(maxsize=2)

    async def on_frame(params):
        data = params.get("data", "")
        session_id = params.get("sessionId", 0)
        # Ack frame so CDP keeps sending
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass
        # Drop frames if consumer is slow
        if not frame_queue.full():
            await frame_queue.put(data)

    cdp.on("Page.screencastFrame", on_frame)

    try:
        await cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": 40,
            "maxWidth": 1280,
            "maxHeight": 720,
            "everyNthFrame": 1,
        })

        while True:
            data = await frame_queue.get()
            await websocket.send_text(data)

    except (WebSocketDisconnect, Exception) as e:
        log.info(f"Screencast WS closed: {e}")
    finally:
        try:
            await cdp.send("Page.stopScreencast")
            await cdp.detach()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# WebSocket Desktop Screencast (PC mode — mss multi-monitor capture)
# ---------------------------------------------------------------------------
@app.websocket("/ws/screencast-desktop")
async def ws_screencast_desktop(websocket: WebSocket):
    """
    Stream entire desktop (all monitors stitched) as JPEG frames.
    Uses mss for multi-monitor screenshots at ~10-15 FPS.
    """
    await websocket.accept()
    log.info("Desktop screencast WS connected")

    try:
        import mss
        import mss.tools
        from io import BytesIO
        from PIL import Image
    except ImportError:
        log.error("Desktop screencast requires 'mss' and 'Pillow'. Install with: pip install mss Pillow")
        await websocket.close(1011, "mss/Pillow not installed")
        return

    try:
        with mss.mss() as sct:
            log.info("Desktop capture started")

            while True:
                # Capture only the selected monitor
                # mss.monitors: [0]=all combined, [1]=primary, [2]=second, ...
                mon = get_active_monitor()
                target = {
                    "left": mon["left"],
                    "top": mon["top"],
                    "width": mon["width"],
                    "height": mon["height"],
                }
                shot = sct.grab(target)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

                # Scale down for bandwidth (max 1920 wide)
                if img.width > 1920:
                    ratio = 1920 / img.width
                    img = img.resize(
                        (1920, int(img.height * ratio)),
                        Image.LANCZOS,
                    )

                buf = BytesIO()
                img.save(buf, format="JPEG", quality=40, optimize=True)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")

                await websocket.send_text(b64)
                await asyncio.sleep(0.07)  # ~14 FPS cap
    except (WebSocketDisconnect, Exception) as e:
        log.info(f"Desktop screencast WS closed: {e}")


# ---------------------------------------------------------------------------
# WebSocket Gyro Cursor (air-mouse velocity-based)
# ---------------------------------------------------------------------------
@app.websocket("/ws/gyro")
async def ws_gyro(websocket: WebSocket):
    """
    Air-mouse gyro: receives {dx, dy} velocity-based deltas from DeviceMotionEvent
    rotationRate. Server accumulates position. Also handles {type:"click"}.
    Works in both Browser mode (Playwright DOM cursor) and PC mode (OS cursor).
    """
    global cursor_pos
    await websocket.accept()
    log.info("Gyro WS connected")

    # Resolve bounds once
    vp = None
    max_x, max_y = 1920.0, 1080.0
    mon_offset_x, mon_offset_y = 0, 0

    if pc_mode:
        mon = get_active_monitor()
        max_x, max_y = float(mon["width"]), float(mon["height"])
        mon_offset_x, mon_offset_y = mon["left"], mon["top"]
        # Init cursor to current OS position
        ox, oy = os_cursor_position()
        cursor_pos["x"], cursor_pos["y"] = float(ox) - mon_offset_x, float(oy) - mon_offset_y
    else:
        try:
            await ensure_connection()
            vp = await get_viewport()
            max_x, max_y = float(vp["width"]), float(vp["height"])
        except Exception as e:
            log.error(f"Gyro WS: initial connection failed: {e}")
            await websocket.close(1011, "Server not connected to browser")
            return

    async def _auto_hide_cursor(delay: float):
        """Background task: hide cursor after `delay` seconds of inactivity."""
        await asyncio.sleep(delay)
        if cursor_pos["visible"]:
            cursor_pos["visible"] = False
            if not pc_mode:
                try:
                    await page.evaluate(CURSOR_REMOVE_JS)
                except Exception:
                    pass
            log.info("Cursor auto-hidden (idle)")
            try:
                await websocket.send_text(json.dumps({"type": "cursor_hidden"}))
            except Exception:
                pass

    async def _ensure_cursor_visible():
        """Re-show cursor if it was auto-hidden."""
        global cursor_hide_task
        nonlocal vp, max_x, max_y
        # Cancel pending hide
        if cursor_hide_task and not cursor_hide_task.done():
            cursor_hide_task.cancel()
        if not cursor_pos["visible"]:
            cursor_pos["visible"] = True
            if pc_mode:
                x, y = os_cursor_position()
                cursor_pos["x"], cursor_pos["y"] = float(x) - mon_offset_x, float(y) - mon_offset_y
            else:
                try:
                    await page.evaluate(CURSOR_INJECT_JS)
                    await page.evaluate(CURSOR_UPDATE_JS, {"x": cursor_pos["x"], "y": cursor_pos["y"]})
                except Exception:
                    pass
            log.info("Cursor auto-shown (movement)")
        # Schedule new hide
        hide_delay = config.load().get("cursor_hide_delay", 2.0)
        cursor_hide_task = asyncio.create_task(_auto_hide_cursor(hide_delay))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                msg_type = data.get("type", "")

                if pc_mode:
                    mon = get_active_monitor()
                    max_x, max_y = float(mon["width"]), float(mon["height"])
                    mon_offset_x, mon_offset_y = mon["left"], mon["top"]

                if msg_type == "click":
                    if pc_mode:
                        os_cursor_click()
                        x, y = os_cursor_position()
                        cursor_pos["x"], cursor_pos["y"] = float(x) - mon_offset_x, float(y) - mon_offset_y
                        log.info(f"Gyro WS PC click ({x}, {y})")
                    else:
                        x, y = cursor_pos["x"], cursor_pos["y"]
                        await page.evaluate(CURSOR_CLICK_RIPPLE_JS, {"x": x, "y": y})
                        await page.mouse.click(x, y)
                        log.info(f"Gyro WS click ({x:.0f}, {y:.0f})")
                    await websocket.send_text(json.dumps({
                        "type": "clicked",
                        "x": round(cursor_pos["x"]),
                        "y": round(cursor_pos["y"]),
                    }))
                elif msg_type == "scroll":
                    sdx = float(data.get("deltaX", 0))
                    sdy = float(data.get("deltaY", 0))
                    if pc_mode:
                        MOUSEEVENTF_WHEEL = 0x0800
                        MOUSEEVENTF_HWHEEL = 0x01000
                        if sdy != 0:
                            ctypes.windll.user32.mouse_event(
                                MOUSEEVENTF_WHEEL, 0, 0, int(-sdy), 0)
                        if sdx != 0:
                            ctypes.windll.user32.mouse_event(
                                MOUSEEVENTF_HWHEEL, 0, 0, int(sdx), 0)
                    else:
                        await page.mouse.move(
                            cursor_pos["x"], cursor_pos["y"])
                        await page.mouse.wheel(sdx, sdy)
                    await websocket.send_text(json.dumps({
                        "type": "scrolled"}))
                else:
                    # Delta-based: {dx, dy} in pixels
                    dx = float(data.get("dx", 0))
                    dy = float(data.get("dy", 0))

                    # Re-show cursor if hidden, reset hide timer
                    await _ensure_cursor_visible()

                    # Accumulate + clamp
                    new_x = cursor_pos["x"] + dx
                    new_y = cursor_pos["y"] + dy
                    clamped = (new_x <= 0 or new_x >= max_x or
                               new_y <= 0 or new_y >= max_y)
                    cursor_pos["x"] = max(0, min(max_x, new_x))
                    cursor_pos["y"] = max(0, min(max_y, new_y))

                    if pc_mode:
                        os_cursor_set(cursor_pos["x"] + mon_offset_x, cursor_pos["y"] + mon_offset_y)
                    else:
                        await page.evaluate(CURSOR_UPDATE_JS, {
                            "x": cursor_pos["x"],
                            "y": cursor_pos["y"],
                        })

                    msg = {
                        "x": round(cursor_pos["x"]),
                        "y": round(cursor_pos["y"]),
                    }
                    if clamped:
                        msg["clamped"] = True
                    await websocket.send_text(json.dumps(msg))
            except WebSocketDisconnect:
                raise
            except Exception as inner:
                log.warning(f"Gyro frame error: {inner}")
                if not pc_mode:
                    try:
                        await ensure_connection()
                        vp = await get_viewport()
                        max_x, max_y = float(vp["width"]), float(vp["height"])
                    except Exception:
                        pass
                continue
    except (WebSocketDisconnect, Exception) as e:
        log.info(f"Gyro WS closed: {e}")
    finally:
        if cursor_hide_task and not cursor_hide_task.done():
            cursor_hide_task.cancel()


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------
@app.post("/api/press/{key}")
async def press_key(key: str):
    await ensure_connection()
    key_map = {
        "up": "ArrowUp", "down": "ArrowDown",
        "left": "ArrowLeft", "right": "ArrowRight",
        "enter": "Enter", "back": "Escape",
        "escape": "Escape", "backspace": "Backspace",
        "space": " ", "playpause": "k",
        "rewind": "j", "forward": "l",
        "fullscreen": "f", "mute": "m", "tab": "Tab",
    }
    resolved = key_map.get(key.lower(), key)
    if resolved not in ALLOWED_KEYS:
        raise HTTPException(400, f"Key '{key}' not allowed")
    await page.keyboard.press(resolved)
    log.info(f"Key: {resolved}")
    return {"status": "ok", "key": resolved}

@app.post("/api/type")
async def type_text(data: TypeText):
    await ensure_connection()
    if len(data.text) > 500:
        raise HTTPException(400, "Text too long")
    await page.keyboard.type(data.text, delay=50)
    log.info(f"Typed: {data.text[:50]}")
    return {"status": "ok", "length": len(data.text)}


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------
@app.post("/api/navigate")
async def navigate(data: Navigate):
    url = data.url
    if url.lower() in config.SITES:
        url = config.SITES[url.lower()]
    await smart_navigate(url)
    return {"status": "ok", "url": url}

@app.get("/api/sites")
async def get_sites():
    return {"sites": config.SITES}

@app.post("/api/go-back")
async def go_back():
    await ensure_connection()
    await page.go_back()
    return {"status": "ok"}

@app.post("/api/go-forward")
async def go_forward():
    await ensure_connection()
    await page.go_forward()
    return {"status": "ok"}

@app.post("/api/reload")
async def reload_page():
    await ensure_connection()
    await page.reload()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------
class SettingsUpdate(BaseModel):
    cursor_hide_delay: Optional[float] = None
    cursor_sensitivity: Optional[float] = None
    scroll_speed: Optional[float] = None


@app.get("/api/settings")
async def get_settings():
    """Return user-facing configurable settings."""
    cfg = config.load()
    return {
        "cursor_hide_delay": cfg.get("cursor_hide_delay", 2.0),
        "cursor_sensitivity": cfg.get("cursor_sensitivity", 1.0),
        "scroll_speed": cfg.get("scroll_speed", 1.0),
        "startup_urls": cfg.get("startup_urls", []),
        "language": cfg.get("language", "en"),
    }


@app.post("/api/settings")
async def update_settings(data: SettingsUpdate):
    """Update configurable settings."""
    updates = {}
    if data.cursor_hide_delay is not None:
        updates["cursor_hide_delay"] = max(0.5, min(10.0, data.cursor_hide_delay))
    if data.cursor_sensitivity is not None:
        updates["cursor_sensitivity"] = max(0.1, min(5.0, data.cursor_sensitivity))
    if data.scroll_speed is not None:
        updates["scroll_speed"] = max(0.1, min(5.0, data.scroll_speed))
    if updates:
        config.save(updates)
        config._reload()
    return {"status": "ok", **updates}


# ---------------------------------------------------------------------------
# Favorites / Bookmarks
# ---------------------------------------------------------------------------
class FavoriteAdd(BaseModel):
    name: str
    url: str

class FavoriteRemove(BaseModel):
    url: str


@app.get("/api/favorites")
async def get_favorites():
    cfg = config.load()
    return {"favorites": cfg.get("favorites", [])}


@app.post("/api/favorites")
async def add_favorite(data: FavoriteAdd):
    cfg = config.load()
    favs = cfg.get("favorites", [])
    # Avoid duplicates by URL
    if not any(f["url"] == data.url for f in favs):
        favs.append({"name": data.name, "url": data.url})
        config.save({"favorites": favs})
    return {"favorites": favs}


@app.delete("/api/favorites")
async def remove_favorite(data: FavoriteRemove):
    cfg = config.load()
    favs = [f for f in cfg.get("favorites", []) if f["url"] != data.url]
    config.save({"favorites": favs})
    return {"favorites": favs}


# ---------------------------------------------------------------------------
# Volume Control (per-session browser audio via pycaw)
# ---------------------------------------------------------------------------
def _get_browser_volume_sync():
    """(Blocking) Get browser audio volume via COM/pycaw. Must run in a worker thread."""
    import comtypes
    comtypes.CoInitialize()
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        sessions = AudioUtilities.GetAllSessions()
        browser_names = {"brave.exe", "chrome.exe", "msedge.exe"}
        cfg = config.load()
        configured = cfg.get("brave_path", "")
        if configured:
            browser_names.add(os.path.basename(configured).lower())
        for s in sessions:
            if s.Process and s.Process.name().lower() in browser_names:
                vol = s._ctl.QueryInterface(ISimpleAudioVolume)
                level = vol.GetMasterVolume()
                muted = vol.GetMute()
                return {"volume": round(level, 2), "muted": bool(muted), "pid": s.Process.pid, "source": "browser"}
        return None
    except Exception as e:
        log.warning(f"Browser audio session lookup failed: {e}")
        return None
    finally:
        comtypes.CoUninitialize()


def _set_browser_volume_sync(volume=None, muted=None):
    """(Blocking) Set browser audio volume via COM/pycaw. Must run in a worker thread."""
    import comtypes
    comtypes.CoInitialize()
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
        sessions = AudioUtilities.GetAllSessions()
        browser_names = {"brave.exe", "chrome.exe", "msedge.exe"}
        cfg = config.load()
        configured = cfg.get("brave_path", "")
        if configured:
            browser_names.add(os.path.basename(configured).lower())
        for s in sessions:
            if s.Process and s.Process.name().lower() in browser_names:
                vol = s._ctl.QueryInterface(ISimpleAudioVolume)
                if volume is not None:
                    vol.SetMasterVolume(max(0.0, min(1.0, volume)), None)
                if muted is not None:
                    vol.SetMute(int(muted), None)
                return {"status": "ok", "volume": round(vol.GetMasterVolume(), 2),
                        "muted": bool(vol.GetMute()), "source": "browser"}
        return None
    except Exception as e:
        log.warning(f"Browser audio session set failed: {e}")
        return None
    finally:
        comtypes.CoUninitialize()


def _get_system_volume_sync():
    """(Blocking) Get system-wide volume via COM/pycaw."""
    import comtypes
    comtypes.CoInitialize()
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        from comtypes import CLSCTX_ALL
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = interface.QueryInterface(IAudioEndpointVolume)
        level = vol.GetMasterVolumeLevelScalar()
        muted = vol.GetMute()
        return {"volume": round(level, 2), "muted": bool(muted), "source": "system"}
    except Exception as e:
        log.warning(f"System volume lookup failed: {e}")
        return None
    finally:
        comtypes.CoUninitialize()


def _set_system_volume_sync(volume=None, muted=None):
    """(Blocking) Set system-wide volume via COM/pycaw."""
    import comtypes
    comtypes.CoInitialize()
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        from comtypes import CLSCTX_ALL
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = interface.QueryInterface(IAudioEndpointVolume)
        if volume is not None:
            vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, volume)), None)
        if muted is not None:
            vol.SetMute(int(muted), None)
        return {"status": "ok", "volume": round(vol.GetMasterVolumeLevelScalar(), 2),
                "muted": bool(vol.GetMute()), "source": "system"}
    except Exception as e:
        log.warning(f"System volume set failed: {e}")
        return None
    finally:
        comtypes.CoUninitialize()


@app.get("/api/volume")
async def get_volume():
    """Get the browser's audio session volume, falling back to system volume."""
    result = await asyncio.to_thread(_get_browser_volume_sync)
    if result is not None:
        return result
    result = await asyncio.to_thread(_get_system_volume_sync)
    if result is not None:
        return result
    return {"volume": -1, "muted": False, "error": "No audio control available"}


class VolumeSet(BaseModel):
    volume: Optional[float] = None
    muted: Optional[bool] = None


@app.post("/api/volume")
async def set_volume(data: VolumeSet):
    """Set the browser's audio session volume (0.0-1.0) or mute state, with system fallback."""
    result = await asyncio.to_thread(_set_browser_volume_sync, data.volume, data.muted)
    if result is not None:
        return result
    result = await asyncio.to_thread(_set_system_volume_sync, data.volume, data.muted)
    if result is not None:
        return result
    raise HTTPException(503, "No audio control available")


# ---------------------------------------------------------------------------
# Audio Output Devices
# ---------------------------------------------------------------------------
@app.get("/api/audio-outputs")
async def list_audio_outputs():
    """List audio output (render) devices."""
    try:
        from pycaw.pycaw import AudioUtilities
        from pycaw.constants import EDataFlow
        devices = AudioUtilities.GetAllDevices()
        outputs = []
        for d in devices:
            try:
                if d.data_flow == EDataFlow.eRender.value:
                    outputs.append({
                        "id": d.id,
                        "name": d.FriendlyName,
                        "state": d.state,
                    })
            except Exception:
                continue
        return {"devices": outputs}
    except ImportError:
        return {"devices": [], "error": "pycaw not installed"}
    except Exception as e:
        return {"devices": [], "error": str(e)}


# ---------------------------------------------------------------------------
# Media Controls (system-level media keys via SendInput)
# ---------------------------------------------------------------------------
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_VOLUME_UP = 0xAF
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_MUTE = 0xAD

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", INPUT_UNION)]


def _send_vk(vk_code: int):
    """Send a virtual key press+release via SendInput."""
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki.wVk = vk_code
    inp.union.ki.dwFlags = KEYEVENTF_EXTENDEDKEY
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
    inp.union.ki.dwFlags = KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


MEDIA_KEY_MAP = {
    "play_pause": VK_MEDIA_PLAY_PAUSE,
    "next": VK_MEDIA_NEXT_TRACK,
    "prev": VK_MEDIA_PREV_TRACK,
    "vol_up": VK_VOLUME_UP,
    "vol_down": VK_VOLUME_DOWN,
    "mute": VK_VOLUME_MUTE,
}


@app.post("/api/media/{action}")
async def media_control(action: str):
    """Send system-level media key. Actions: play_pause, next, prev, vol_up, vol_down, mute"""
    vk = MEDIA_KEY_MAP.get(action)
    if vk is None:
        raise HTTPException(400, f"Unknown media action: {action}. Use: {list(MEDIA_KEY_MAP.keys())}")
    _send_vk(vk)
    log.info(f"Media key: {action}")
    return {"status": "ok", "action": action}


# ---------------------------------------------------------------------------
# Keyboard Mode (full key input)
# ---------------------------------------------------------------------------
KEYEVENTF_UNICODE = 0x0004

# VK codes for common special keys
VK_MAP = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "alt": 0x12, "escape": 0x1B, "space": 0x20,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "win": 0x5B, "printscreen": 0x2C,
}

# Expanded allowed keys for keyboard mode (browser mode)
KEYBOARD_ALLOWED = ALLOWED_KEYS | {
    "Delete", "Home", "End", "PageUp", "PageDown", "Insert",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
}


def _os_send_unicode(text: str):
    """Send unicode text via SendInput."""
    for ch in text:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wScan = ord(ch)
        inp.union.ki.dwFlags = KEYEVENTF_UNICODE
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        inp.union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _os_send_combo(modifiers: list[int], vk: int):
    """Send a key combination (e.g., Ctrl+C) via SendInput."""
    # Press modifiers
    for mod in modifiers:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = mod
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
    # Press+release main key
    _send_vk(vk)
    # Release modifiers in reverse
    for mod in reversed(modifiers):
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = mod
        inp.union.ki.dwFlags = KEYEVENTF_KEYUP
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


class KeyboardInput(BaseModel):
    text: Optional[str] = None
    key: Optional[str] = None
    modifiers: Optional[list[str]] = None  # ["ctrl", "shift", "alt"]


@app.post("/api/keyboard")
async def keyboard_input(data: KeyboardInput):
    """Full keyboard input. Send text or key presses with optional modifiers."""
    if data.text:
        if len(data.text) > 500:
            raise HTTPException(400, "Text too long")
        if pc_mode:
            _os_send_unicode(data.text)
        else:
            await ensure_connection()
            await page.keyboard.type(data.text, delay=30)
        log.info(f"KB type: {data.text[:30]}")
        return {"status": "ok", "typed": len(data.text)}

    if data.key:
        key_lower = data.key.lower()
        mods = [m.lower() for m in (data.modifiers or [])]

        if pc_mode:
            # OS-level key press
            mod_vks = []
            if "ctrl" in mods:
                mod_vks.append(0x11)
            if "shift" in mods:
                mod_vks.append(0x10)
            if "alt" in mods:
                mod_vks.append(0x12)
            vk = VK_MAP.get(key_lower)
            if vk:
                if mod_vks:
                    _os_send_combo(mod_vks, vk)
                else:
                    _send_vk(vk)
            elif len(data.key) == 1:
                # Single character
                vk = ctypes.windll.user32.VkKeyScanW(ord(data.key))
                if vk != -1:
                    if mod_vks:
                        _os_send_combo(mod_vks, vk & 0xFF)
                    else:
                        _send_vk(vk & 0xFF)
                else:
                    _os_send_unicode(data.key)
            else:
                raise HTTPException(400, f"Unknown key: {data.key}")
        else:
            # Browser mode via Playwright
            await ensure_connection()
            # Build Playwright key string
            key_map_pw = {
                "backspace": "Backspace", "tab": "Tab", "enter": "Enter",
                "escape": "Escape", "space": " ", "delete": "Delete",
                "home": "Home", "end": "End", "pageup": "PageUp",
                "pagedown": "PageDown", "insert": "Insert",
                "left": "ArrowLeft", "up": "ArrowUp",
                "right": "ArrowRight", "down": "ArrowDown",
                "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
                "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8",
                "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
            }
            resolved = key_map_pw.get(key_lower, data.key)
            combo_parts = []
            if "ctrl" in mods:
                combo_parts.append("Control")
            if "shift" in mods:
                combo_parts.append("Shift")
            if "alt" in mods:
                combo_parts.append("Alt")
            combo_parts.append(resolved)
            combo_str = "+".join(combo_parts)
            await page.keyboard.press(combo_str)

        log.info(f"KB key: {'+'.join(mods + [data.key]) if mods else data.key}")
        return {"status": "ok", "key": data.key, "modifiers": mods}

    raise HTTPException(400, "Provide 'text' or 'key'")




# ---------------------------------------------------------------------------
# Dashboard (connection info for launcher)
# ---------------------------------------------------------------------------
_connected_clients: dict[str, float] = {}  # ws_id -> last_seen


@app.get("/api/dashboard")
async def dashboard():
    """Return server status info for the launcher dashboard."""
    import platform
    now = time.time()
    # Clean stale clients (>30s)
    stale = [k for k, v in _connected_clients.items() if now - v > 30]
    for k in stale:
        _connected_clients.pop(k, None)

    connected = False
    title = url = ""
    if page:
        try:
            title = await page.title()
            url = page.url
            connected = True
        except Exception:
            pass

    return {
        "server_uptime": now,
        "connected_to_browser": connected,
        "browser_title": title,
        "browser_url": url,
        "active_clients": len(_connected_clients),
        "pc_mode": pc_mode,
        "pc_monitor": pc_monitor,
        "monitors": detect_monitors(),
        "platform": platform.platform(),
        "python": platform.python_version(),
    }


# ---------------------------------------------------------------------------
# Startup URLs (open as additional tabs after browser launch)
# ---------------------------------------------------------------------------
async def _open_startup_urls():
    """Open configured startup URLs as new tabs."""
    cfg = config.load()
    urls = cfg.get("startup_urls", [])
    if not urls or page is None:
        return
    for url in urls:
        try:
            ctx = browser.contexts[0] if browser and browser.contexts else None
            if ctx:
                new_page = await ctx.new_page()
                await new_page.goto(url, wait_until="domcontentloaded", timeout=15000)
                log.info(f"Opened startup URL: {url}")
        except Exception as e:
            log.warning(f"Failed to open startup URL {url}: {e}")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app", 
        host=config.HOST, 
        port=config.PORT, 
        reload=False,
        ssl_keyfile="key.pem",
        ssl_certfile="cert.pem"
    )
