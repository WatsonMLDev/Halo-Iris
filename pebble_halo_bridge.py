"""
pebble_halo_bridge.py — End-to-End PebbleOS -> Brilliant Labs Halo Bridge PoC!

Architecture:
1. Starts an embedded HTTP server (with COOP/COEP headers) serving pebble-qemu-wasm.
2. Boots real PebbleOS firmware (`gabbro` 260x260 circular Pebble Round 2, or
   `chalk` 180x180 Pebble Time Round, or `emery`) inside Playwright Chromium.
3. Hooks the PebbleOS framebuffer in JS and streams changed frames to Python.
4. Diffs frames to compute the dirty bounding box, quantizes to 1/2/4-bit indexed
   color, LZ4-compresses, and sends over BrilliantMsg (`0x22` PEBBLE_RECT_FLAG)
   to `lua/pebble_halo_app.lua` running on `HaloEmulator` (or real Halo glasses!).
5. Forwards Halo button clicks & IMU temple taps back into PebbleOS hardware buttons:
   - Single Click (SPACE in Pygame)  -> Pebble SELECT
   - Double Click (D in Pygame)      -> Pebble BACK
   - Single Tap   (T in Pygame)      -> Pebble DOWN
   - Double Tap   (U in Pygame)      -> Pebble UP
   - Arrow Keys in Pygame            -> Direct Pebble UP / DOWN / LEFT(Back) / RIGHT(Select)
6. Supports installing any `.pbw` file or Rebble App Store URL live onto the watch!

Usage:
  # Headless quick verification (boots Gabbro circular PebbleOS, installs test .pbw or
  # navigates the PebbleOS launcher, saves Halo display screenshots):
  python pebble_halo_bridge.py --demo-headless

  # Interactive live mode (opens the Halo circular HUD Pygame window + optional
  # Pebble phone companion window so you can paste Rebble store links!):
  python pebble_halo_bridge.py --board gabbro
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import http.server
import io
import os
import socketserver
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.async_api import async_playwright

from halo_emulator import HaloEmulator, EmulatorBrilliantMsg
from pebble_frame_encoder import (
    PEBBLE_RECT_FLAG,
    compute_dirty_bbox,
    encode_pebble_rect,
    normalize_pebble_frame,
)

POC_DIR = Path(__file__).parent.resolve()
WASM_DIR = POC_DIR / "pebble-qemu-wasm"
LUA_APP = POC_DIR / "lua" / "pebble_halo_app.lua"

CLICK_FLAG = 0x0B
TAP_FLAG = 0x09


def start_coop_coep_server(directory: Path, port: int = 8089) -> socketserver.TCPServer:
    """Start a background HTTP server with COOP/COEP headers for SharedArrayBuffer."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def end_headers(self):
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Embedder-Policy", "require-corp")
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def log_message(self, format, *args):
            pass  # keep console clean

    class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = ThreadedServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True, name="wasm-http").start()
    return httpd


def run_halo_pygame_window(
    emu: HaloEmulator,
    button_queue: asyncio.Queue[str],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """
    Render the Halo 256x256 circular display in a 2x scaled Pygame window.
    Lets you test both Halo physical gestures AND direct Pebble buttons:
      SPACE = Halo Single Click (Pebble Select)
      D     = Halo Double Click (Pebble Back)
      T     = Halo Single Tap   (Pebble Down)
      U     = Halo Double Tap   (Pebble Up)
      Arrow Keys = Direct Pebble Up / Down / Left(Back) / Right(Select)
    """
    os.environ.setdefault("SDL_VIDEO_WINDOW_POS", "80,80")
    import pygame

    SCALE = 2
    W, H = 256 * SCALE, 256 * SCALE

    pygame.display.init()
    screen = pygame.display.set_mode((W, H))
    pygame.display.set_caption("Halo Glasses HUD — PebbleOS Bridge (SPACE=Select, D=Back, T=Down, U=Up)")
    clock = pygame.time.Clock()

    # Circular lens mask matching Halo optics
    mask = pygame.Surface((W, H), pygame.SRCALPHA)
    mask.fill((20, 20, 25, 255))
    pygame.draw.circle(mask, (0, 0, 0, 0), (W // 2, H // 2), W // 2)
    pygame.draw.circle(mask, (76, 201, 240, 120), (W // 2, H // 2), W // 2, 2)

    while emu.is_running():
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                emu.stop()
                return
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_SPACE:
                    emu.inject_button_single()
                elif event.key == pygame.K_d:
                    emu.inject_button_double()
                elif event.key == pygame.K_l:
                    emu.inject_button_long()
                elif event.key == pygame.K_t:
                    emu.inject_imu_tap("single")
                elif event.key == pygame.K_u:
                    emu.inject_imu_tap("double")
                elif event.key == pygame.K_UP:
                    loop.call_soon_threadsafe(button_queue.put_nowait, "up")
                elif event.key == pygame.K_DOWN:
                    loop.call_soon_threadsafe(button_queue.put_nowait, "down")
                elif event.key in (pygame.K_RIGHT, pygame.K_RETURN):
                    loop.call_soon_threadsafe(button_queue.put_nowait, "select")
                elif event.key in (pygame.K_LEFT, pygame.K_BACKSPACE, pygame.K_ESCAPE):
                    loop.call_soon_threadsafe(button_queue.put_nowait, "back")

        img = emu.get_framebuffer()
        surface = pygame.image.fromstring(img.tobytes(), (256, 256), img.mode)
        scaled = pygame.transform.scale(surface, (W, H))
        scaled.blit(mask, (0, 0))
        screen.blit(scaled, (0, 0))
        pygame.display.flip()
        clock.tick(30)

    pygame.quit()


async def main() -> None:
    parser = argparse.ArgumentParser(description="PebbleOS -> Halo Glasses Bridge PoC")
    parser.add_argument(
        "--board",
        default="gabbro",
        choices=["gabbro", "chalk", "emery", "basalt", "aplite", "diorite", "flint"],
        help="Pebble board to emulate (default: gabbro = 260x260 circular Pebble Round 2)",
    )
    parser.add_argument(
        "--pbw",
        default=None,
        help="Optional path to a .pbw file OR apps.repebble.com URL to auto-install after boot",
    )
    parser.add_argument(
        "--build-zip",
        default=None,
        help="Optional zip URL (e.g. TouchyWeather.zip) or GitHub repo URL to compile with clang.wasm and auto-install",
    )
    parser.add_argument(
        "--scale-chalk",
        action="store_true",
        help="Scale 180x180 Chalk display up to 256x256 (default centers 180x180 inside 256x256)",
    )
    parser.add_argument(
        "--demo-headless",
        action="store_true",
        help="Run an automated headless test: boot PebbleOS, stream frames to HaloEmulator, press buttons, and save screenshots",
    )
    parser.add_argument(
        "--show-browser",
        action="store_true",
        help="Show the companion Chromium window (lets you paste store URLs or click phone controls)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8089,
        help="Local port for COOP/COEP WASM server (default: 8089)",
    )
    args = parser.parse_args()

    # 1. Start local COOP/COEP server for pebble-qemu-wasm
    httpd = start_coop_coep_server(WASM_DIR, port=args.port)
    wasm_url = f"http://127.0.0.1:{args.port}/index.html?board={args.board}&loc=37.7749,-122.4194"
    print(f"[1/4] Serving pebble-qemu-wasm at {wasm_url}")

    # 2. Boot HaloEmulator and load pebble_halo_app.lua
    emu = HaloEmulator()
    frame = EmulatorBrilliantMsg(emu)
    button_queue: asyncio.Queue[str] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # Map Halo button clicks & temple taps -> Pebble buttons
    def on_halo_click(data: bytes) -> None:
        click_type = data[1]
        if click_type == 1:
            print("  [Halo Input] Single Click -> Pebble SELECT")
            loop.call_soon_threadsafe(button_queue.put_nowait, "select")
        elif click_type == 2:
            print("  [Halo Input] Double Click -> Pebble BACK")
            loop.call_soon_threadsafe(button_queue.put_nowait, "back")
        elif click_type == 3:
            print("  [Halo Input] Long Press   -> Pebble BACK")
            loop.call_soon_threadsafe(button_queue.put_nowait, "back")

    def on_halo_tap(data: bytes) -> None:
        tap_kind = data[1]
        if tap_kind == 1:
            print("  [Halo Input] Single Tap   -> Pebble DOWN")
            loop.call_soon_threadsafe(button_queue.put_nowait, "down")
        elif tap_kind in (2, 3):
            print("  [Halo Input] Double Tap   -> Pebble UP")
            loop.call_soon_threadsafe(button_queue.put_nowait, "up")

    try:
        await frame.connect()
        await frame.upload_stdlua_libs(lib_names=["data"])
        await frame.upload_frame_app(
            local_filename=str(LUA_APP),
            frame_filename="pebble_halo_app.lua",
        )
        frame.attach_print_response_handler(lambda s: print(f"  [Halo Lua] {s}"))
        await frame.start_frame_app(frame_app_name="pebble_halo_app", await_print=True)
        frame.register_data_response_handler(on_halo_click, [CLICK_FLAG], on_halo_click)
        frame.register_data_response_handler(on_halo_tap, [TAP_FLAG], on_halo_tap)
        print("[2/4] HaloEmulator booted with pebble_halo_app.lua")

        if not args.demo_headless:
            threading.Thread(
                target=run_halo_pygame_window,
                args=(emu, button_queue, loop),
                daemon=True,
                name="halo-pygame",
            ).start()

        # 3. Launch Playwright Chromium to host PebbleOS WASM
        async with async_playwright() as pw:
            exe_path = "/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None
            browser = await pw.chromium.launch(
                executable_path=exe_path,
                headless=not args.show_browser,
                args=["--enable-features=SharedArrayBuffer", "--no-sandbox"],
            )
            context = await browser.new_context()
            page = await context.new_page()

            watch_connected = asyncio.Event()
            display_active = asyncio.Event()

            def on_console(msg):
                text = msg.text
                if "Display active:" in text and not display_active.is_set():
                    print(f"[3/4] PebbleOS {text}")
                    display_active.set()
                elif "Connected to the watch" in text and not watch_connected.is_set():
                    print("[4/4] Pebble Phone Protocol Bridge connected!")
                    watch_connected.set()
                elif "[install]" in text and "Connected to the watch" not in text:
                    print(f"  {text}")
                elif "[build]" in text or "[pkjs]" in text:
                    print(f"  {text}")

            page.on("console", on_console)
            print(f"[3/4] Booting PebbleOS ({args.board}) in WASM QEMU...")
            await page.goto(wasm_url, timeout=60000)

            # Wait for first display frame from PebbleOS
            await asyncio.wait_for(display_active.wait(), timeout=120.0)

            prev_halo_rgb: np.ndarray | None = None
            last_wasm_fc = -1
            frames_streamed = 0

            async def sync_pebble_display_to_halo() -> bool:
                """Check if PebbleOS rendered a new frame; if so, diff & stream to Halo."""
                nonlocal prev_halo_rgb, last_wasm_fc, frames_streamed

                fc = await page.evaluate(
                    "() => window.qemu ? window.qemu._pebble_wasm_display_frame_count() : -1"
                )
                if fc <= 0 or fc == last_wasm_fc:
                    return False
                last_wasm_fc = fc

                data_url = await page.evaluate(
                    "() => document.getElementById('canvas').toDataURL('image/png')"
                )
                if not data_url or not data_url.startswith("data:image/png;base64,"):
                    return False

                png_bytes = base64.b64decode(data_url.split(",", 1)[1])
                pebble_img = Image.open(io.BytesIO(png_bytes))
                halo_img = normalize_pebble_frame(pebble_img, scale_chalk=args.scale_chalk)
                curr_rgb = np.array(halo_img, dtype=np.uint8)

                bbox = compute_dirty_bbox(prev_halo_rgb, curr_rgb)
                if bbox is None:
                    return False

                payload, stats = encode_pebble_rect(curr_rgb, bbox)
                prev_halo_rgb = curr_rgb
                frames_streamed += 1

                hx, hy, bw, bh = stats["bbox"]
                print(
                    f"  [BLE -> Halo #{frames_streamed:03d}] rect=({hx},{hy} {bw}x{bh}) "
                    f"{stats['bpp']}bpp ({stats['num_colors']} colors) "
                    f"raw={stats['raw_bytes']}B -> sent={stats['payload_bytes']}B "
                    f"(lz4={stats['compressed']})"
                )
                await frame.send_message(PEBBLE_RECT_FLAG, payload)
                return True

            async def press_pebble_button(btn_name: str, hold_ms: int = 180) -> None:
                """Press and release a button on the emulated Pebble watch."""
                key_for_btn = {
                    "up": "ArrowUp",
                    "down": "ArrowDown",
                    "select": "Enter",
                    "back": "Escape",
                }[btn_name]
                await page.keyboard.down(key_for_btn)
                await asyncio.sleep(hold_ms / 1000.0)
                await page.keyboard.up(key_for_btn)

            if args.demo_headless:
                print("\n--- Running Automated Headless End-to-End Demo ---")
                # Stream boot & watchface frames for a few seconds until serial phone connects
                t0 = time.time()
                while time.time() - t0 < 25.0:
                    await sync_pebble_display_to_halo()
                    if watch_connected.is_set() and frames_streamed >= 2:
                        break
                    await asyncio.sleep(0.25)

                await asyncio.sleep(2.0)
                await sync_pebble_display_to_halo()
                out_boot = POC_DIR / f"halo_pebble_{args.board}_01_watchface.png"
                frame.get_framebuffer().save(out_boot)
                print(f"Saved Halo framebuffer -> {out_boot.name}")

                # Optionally build from source zip/repo using clang.wasm!
                if args.build_zip:
                    print(f"\nCompiling {args.build_zip} with in-browser clang.wasm for {args.board}...")
                    await page.fill("#build-input", args.build_zip)
                    await page.click("#btn-build")
                    t_build = time.time()
                    while time.time() - t_build < 180.0:
                        await sync_pebble_display_to_halo()
                        status_txt = await page.inner_text("#install-status")
                        if "Installed and launching:" in status_txt:
                            break
                        if "Build failed:" in status_txt or "Install failed:" in status_txt:
                            print(f"ERROR: {status_txt}")
                            break
                        await asyncio.sleep(0.5)

                    # Give TouchyWeather time to launch, receive Open-Meteo PKJS data, and render
                    print("Waiting for app to launch and receive PebbleKit JS weather data...")
                    for _ in range(30):
                        await asyncio.sleep(0.4)
                        await sync_pebble_display_to_halo()
                    out_built = POC_DIR / f"halo_pebble_{args.board}_02_built_app.png"
                    frame.get_framebuffer().save(out_built)
                    print(f"Saved Halo framebuffer -> {out_built.name}")

                    raw_url = await page.evaluate(
                        "() => document.getElementById('canvas').toDataURL('image/png')"
                    )
                    if raw_url and raw_url.startswith("data:image/png;base64,"):
                        raw_png = base64.b64decode(raw_url.split(",", 1)[1])
                        (POC_DIR / f"pebble_raw_{args.board}_02_built_app.png").write_bytes(raw_png)

                    # Dismiss "What's New" modal if open (via Halo Double-Click -> Pebble BACK),
                    # then navigate through TouchyWeather cards via Halo Temple Taps -> Pebble DOWN!
                    for step_idx, (act_name, inject_fn) in enumerate([
                        ("Halo Double-Click (BACK -> Dismiss 'What's New' to Main Weather Card)", emu.inject_button_double),
                        ("Halo Temple Tap (DOWN -> Card 1: Touch & Go Advice)", lambda: emu.inject_imu_tap("single")),
                        ("Halo Temple Tap (DOWN -> Card 2: 6-Hour Forecast)", lambda: emu.inject_imu_tap("single")),
                        ("Halo Temple Tap (DOWN -> Card 3: Weekly Forecast)", lambda: emu.inject_imu_tap("single")),
                        ("Halo Temple Tap (DOWN -> Card 4: Precipitation / UV)", lambda: emu.inject_imu_tap("single")),
                    ], start=3):
                        print(f"\nSimulating {act_name}...")
                        inject_fn()
                        btn = await asyncio.wait_for(button_queue.get(), timeout=2.0)
                        await press_pebble_button(btn)
                        for _ in range(15):
                            await asyncio.sleep(0.35)
                            await sync_pebble_display_to_halo()
                        out_step = POC_DIR / f"halo_pebble_{args.board}_{step_idx:02d}_card.png"
                        frame.get_framebuffer().save(out_step)
                        print(f"Saved Halo framebuffer -> {out_step.name}")

                    print("\n=== Headless PoC Complete! ===")
                    await browser.close()
                    return

                # Optionally install a .pbw if specified
                if args.pbw:
                    pbw_path = Path(args.pbw)
                    if pbw_path.exists():
                        print(f"Uploading local PBW: {pbw_path}...")
                        file_input = await page.query_selector("#upload-file")
                        await file_input.set_input_files(str(pbw_path.resolve()))
                    else:
                        print(f"Installing PBW from store URL: {args.pbw}...")
                        await page.fill("#install-input", args.pbw)
                        await page.click("#btn-install")

                    # Stream frames during install + launch
                    t_inst = time.time()
                    while time.time() - t_inst < 35.0:
                        await sync_pebble_display_to_halo()
                        status_txt = await page.inner_text("#install-status")
                        if "Installed and launching:" in status_txt:
                            break
                        await asyncio.sleep(0.4)
                    for _ in range(15):
                        await asyncio.sleep(0.3)
                        await sync_pebble_display_to_halo()
                    out_app = POC_DIR / f"halo_pebble_{args.board}_02_installed_app.png"
                    frame.get_framebuffer().save(out_app)
                    print(f"Saved Halo framebuffer -> {out_app.name}")

                    # Save the raw Pebble canvas too for side-by-side comparison
                    raw_url = await page.evaluate(
                        "() => document.getElementById('canvas').toDataURL('image/png')"
                    )
                    if raw_url and raw_url.startswith("data:image/png;base64,"):
                        raw_png = base64.b64decode(raw_url.split(",", 1)[1])
                        (POC_DIR / f"pebble_raw_{args.board}_02_installed_app.png").write_bytes(raw_png)

                # Test Halo physical button -> Pebble SELECT (opens Pebble Launcher Menu!)
                print("\nSimulating Halo Single-Click -> Pebble SELECT button...")
                emu.inject_button_single()
                btn = await asyncio.wait_for(button_queue.get(), timeout=2.0)
                await press_pebble_button(btn)

                for _ in range(12):
                    await asyncio.sleep(0.25)
                    await sync_pebble_display_to_halo()

                out_menu = POC_DIR / f"halo_pebble_{args.board}_03_launcher_menu.png"
                frame.get_framebuffer().save(out_menu)
                print(f"Saved Halo framebuffer -> {out_menu.name}")

                # Test Halo IMU Temple Tap -> Pebble DOWN (scrolls Launcher Menu!)
                print("\nSimulating Halo Temple Single-Tap -> Pebble DOWN button...")
                emu.inject_imu_tap("single")
                btn = await asyncio.wait_for(button_queue.get(), timeout=2.0)
                await press_pebble_button(btn)

                for _ in range(12):
                    await asyncio.sleep(0.25)
                    await sync_pebble_display_to_halo()

                out_scroll = POC_DIR / f"halo_pebble_{args.board}_04_menu_scrolled.png"
                frame.get_framebuffer().save(out_scroll)
                print(f"Saved Halo framebuffer -> {out_scroll.name}")
                print("\n=== Headless PoC Complete! ===")
            else:
                # Interactive loop: wait for phone protocol bridge if auto-installing/building
                if args.build_zip or args.pbw:
                    print("Waiting for Pebble Phone Protocol Bridge before auto-installing...")
                    try:
                        await asyncio.wait_for(watch_connected.wait(), timeout=60.0)
                    except asyncio.TimeoutError:
                        print("Warning: Watch connection timed out, continuing to interactive loop.")

                if args.build_zip and watch_connected.is_set():
                    print(f"Auto-building & installing {args.build_zip}...")
                    await page.fill("#build-input", args.build_zip)
                    await page.click("#btn-build")
                elif args.pbw and watch_connected.is_set():
                    pbw_path = Path(args.pbw)
                    if pbw_path.exists():
                        file_input = await page.query_selector("#upload-file")
                        await file_input.set_input_files(str(pbw_path.resolve()))
                    else:
                        await page.fill("#install-input", args.pbw)
                        await page.click("#btn-install")

                print("\n=== Live Bridge Running! ===")
                print("Interact in the Halo HUD window:")
                print("  SPACE = Halo Single Click (Pebble Select)")
                print("  D     = Halo Double Click (Pebble Back)")
                print("  T     = Halo Single Tap   (Pebble Down)")
                print("  U     = Halo Double Tap   (Pebble Up)")
                print("  Arrows = Direct Pebble Up/Down/Left/Right")
                while emu.is_running():
                    await sync_pebble_display_to_halo()
                    while not button_queue.empty():
                        btn = button_queue.get_nowait()
                        await press_pebble_button(btn)
                    await asyncio.sleep(0.05)

            await browser.close()

    finally:
        httpd.shutdown()
        await frame.stop_frame_app()
        await frame.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
