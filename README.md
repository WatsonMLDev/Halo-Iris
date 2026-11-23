# Halo-Iris — PebbleOS × Brilliant Labs Halo Bridge

> **Halo-Iris** *(Path 1 — Optical Projection / BLE Stream)* is part of a two-part exploration into running the Pebble smartwatch ecosystem on Brilliant Labs Halo smart glasses (alongside its upcoming native firmware sibling, **Halo-Atlas**).

Run unmodified Pebble smartwatch apps (`.pbw` binaries from the Rebble App Store or compiled on the fly from GitHub C source repositories) and render them live onto **Brilliant Labs Halo** smart glasses (`256×256` circular OLEDoS display) with bidirectional input mapping (Halo physical button clicks + IMU temple taps → Pebble 4-button controls).

---

## 0. Quick Setup Guide (Fresh Git Clone)

This repository purposefully **does not bundle** third-party repositories (`brilliant_sdk`, `pebble-qemu-wasm`, or sample apps like `TouchyWeather`). They are listed in [`.gitignore`](./.gitignore) and fetched automatically by [`setup.sh`](./setup.sh).

### Prerequisites
* **[`uv`](https://docs.astral.sh/uv/)** (Python package & environment manager — automatically manages Python 3.10+ and `.venv`)
* **Git**
* **Chromium** (system `/usr/bin/chromium`, or `setup.sh` will install Playwright's bundled Chromium automatically)
* *(Optional)* **Node.js / npm** (only needed if you want to bundle `@rebble/clay` locally for `TouchyWeather`)

### One-Command Bootstrap

```bash
git clone git@github.com:WatsonMLDev/Halo-Iris.git
cd Halo-Iris

# Clones brilliant_sdk + pebble-qemu-wasm, patches clang.wasm to -Oz,
# and runs `uv sync` to build the environment:
./setup.sh

# Or include TouchyWeather pre-packaged for in-browser C compilation:
./setup.sh --with-touchyweather
```

### Manual Setup Steps (If You Prefer Running Commands Individually)

1. **Clone External Dependencies:**
   ```bash
   git clone https://github.com/brilliantlabsAR/brilliant_sdk.git
   git clone https://github.com/ericmigi/pebble-qemu-wasm.git
   ```
2. **Apply the `clang.wasm` `-Oz` Size Patch (for large 64 KB Pebble apps):**
   ```bash
   sed -i "s/'-Os'/'-Oz'/g" pebble-qemu-wasm/appbuild/appbuilder.js
   sed -i "s/alsoPlatforms: \['emery', 'flint', 'gabbro', 'aplite', 'basalt', 'chalk', 'diorite'\]/alsoPlatforms: []/g" pebble-qemu-wasm/index.html
   ```
3. **Sync the `uv` Environment:**
   ```bash
   uv sync
   ```
4. **Verify Setup (~2 seconds):**
   ```bash
   uv run python test_encoder_pipeline.py
   ```

---

## 1. Why This Approach Wins

Pebble apps are compiled ARM Cortex-M3/M4 binaries (`pebble-app.bin`) packaged inside `.pbw` zip bundles alongside resource packs (`app_resources.pbpack`) and phone-side JavaScript (`pebble-js-app.js` / PebbleKit JS for weather, web APIs, and Clay settings pages).

Rather than hunting for open-source C code and rewriting every app for Halo's Lua API, or writing a custom `<pebble.h>` emulator from scratch, we **plug Halo directly into the existing Pebble phone companion architecture**:

1. **Full Ecosystem Compatibility (`pebble-qemu-wasm`):**
   We run authentic PebbleOS firmware inside WebAssembly QEMU (`ericmigi/pebble-qemu-wasm`), including its in-browser **Pebble Phone Protocol bridge** (`BlobDB`, `AppFetch`, `PutBytes`, `AppMessage`), **PebbleKit JS** sandbox (`PkjsRuntime`), and in-browser **`clang.wasm` + `lld.wasm` compiler**.
2. **Near-1:1 Circular Display Geometry:**
   * **Halo Display:** `256×256` square framebuffer viewed through a **circular optical aperture**.
   * **Pebble Round 2 (`gabbro`):** `260×260` circular color display. Center-cropping 2 pixels off each edge produces a **1:1 native pixel match (`256×256`)** with zero scaling distortion!
   * **Pebble Time Round (`chalk`):** `180×180` circular color display (can either be centered inside `256×256` with a 38px margin or scaled via `--scale-chalk`).
3. **Beating the BLE Bandwidth Bottleneck (Dirty-Rectangle Diffing + Adaptive Bit-Depth + LZ4):**
   * Streaming a raw `256×256` full-color frame over Bluetooth LE (512-byte MTU) every tick would bottleneck at 1–3 FPS.
   * Instead, [`pebble_frame_encoder.py`](./pebble_frame_encoder.py) masks out pixels outside the `256×256` circular lens, compares consecutive frames (`compute_dirty_bbox`), and encodes **only the changed bounding box (`x, y, w, h`)**.
   * It dynamically selects **1-bit (2 colors)**, **2-bit (4 colors)**, or **4-bit (16 colors)** indexed palettes (reserving index `0` for black/transparent) and applies **LZ4 frame compression** when beneficial.
   * **Measured BLE Payload Sizes from Our Tests:**
     * Idle watchface / menu (no pixel change): **0 bytes sent**
     * Progress bar / small UI tick (`8×6` to `38×4`): **27 – 60 bytes** (1 BLE packet)
     * `TouchyWeather` 10 Hz rotating sun animation (`33×33` dirty rect): **249 – 338 bytes** (1 BLE packet!)
     * Analog watchface second-hand sweep (`130×56` dirty rect): **675 bytes** (2 BLE packets)
     * Full `256×256` 16-color screen transition: **~1.1 KB – 3.5 KB** LZ4-compressed (down from 32,768 bytes raw)

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Host / Phone Companion Bridge (Python PoC or Flutter Android App)          │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  1. Embedded PebbleOS Runtime (pebble-qemu-wasm in Chromium/WebView)  │  │
│  │     • Boots real PebbleOS firmware (gabbro 260x260 / chalk 180x180)   │  │
│  │     • Sideloads any .pbw binary, Rebble Store link, or GitHub C repo  │  │
│  │     • Runs PebbleKit JS (Open-Meteo weather, Clay config pages, etc.) │  │
│  └───────────────────────────────────┬───────────────────────────────────┘  │
│                                      │                                      │
│                        (Polls _pebble_wasm_display_frame_count)             │
│                                      ▼                                      │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  2. Frame Diff & Encoder (pebble_frame_encoder.py)                    │  │
│  │     • Center-crops 260x260 -> 256x256 & masks circular lens corners   │  │
│  │     • Diffs against previous frame -> (x, y, width, height) dirty box │  │
│  │     • Quantizes to 1bpp / 2bpp / 4bpp custom palette + LZ4 compress   │  │
│  └───────────────────────────────────┬───────────────────────────────────┘  │
│                                      │                                      │
│  ┌───────────────────────────────────▼───────────────────────────────────┐  │
│  │  3. Brilliant SDK Transport (EmulatorBrilliantMsg / BrilliantMsg)     │  │
│  │     • Sends 0x22 (PEBBLE_RECT_FLAG) packets to Halo                   │  │
│  │     • Receives 0x0B (Button Clicks) & 0x09 (IMU Taps) from Halo       │  │
│  │       and injects Select / Back / Up / Down into PebbleOS             │  │
│  └───────────────────────────────────┬───────────────────────────────────┘  │
└──────────────────────────────────────┼──────────────────────────────────────┘
                                       │ BLE (or HaloEmulator Queue)
                                       ▼
             ┌───────────────────────────────────────────────────┐
             │  Brilliant Labs Halo Glasses (pebble_halo_app.lua)│
             │  • ~110-line Lua 5.4 script                       │
             │  • Decompresses LZ4 blocks & draws sub-bitmaps    │
             │    via frame.display.bitmap(x, y, w, fmt, ...)    │
             │  • Forwards button.single/double/long & imu.tap   │
             └───────────────────────────────────────────────────┘
```

---

## 3. Repository Layout (`Halo-Iris`)

| Path | Description |
|------|-------------|
| [`setup.sh`](./setup.sh) | One-command setup script that clones external repos (`brilliant_sdk`, `pebble-qemu-wasm`, optional `TouchyWeather`), patches `clang.wasm` to `-Oz`, and runs `uv sync`. |
| [`pyproject.toml`](./pyproject.toml) / [`uv.lock`](./uv.lock) | `uv` project definition wiring the local editable `brilliant_sdk` packages (`brilliant-ble`, `brilliant-msg`, `brilliant-sdk`, `halo-emulator`) and host dependencies (`playwright`, `lz4`, `pillow`, `numpy`, `aiohttp`, `websockets`). |
| [`lua/pebble_halo_app.lua`](./lua/pebble_halo_app.lua) | The Lua app that runs on Halo (or `HaloEmulator`). Receives `0x22` dirty-rect/full-frame packets, handles multi-block LZ4 decompression, renders indexed bitmaps with custom RGB palettes, and forwards physical button clicks (`0x0B`) and IMU temple taps (`0x09`). |
| [`pebble_frame_encoder.py`](./pebble_frame_encoder.py) | Converts Pebble frames (`260×260` `gabbro`, `180×180` `chalk`, etc.) to `256×256` circular Halo frames, computes minimal dirty bounding boxes, quantizes to 1/2/4-bit palettes, and packs the 10-byte `0x22` header + palette + LZ4 payload. |
| [`pebble_halo_bridge.py`](./pebble_halo_bridge.py) | End-to-end orchestrator: starts the COOP/COEP HTTP server for `pebble-qemu-wasm`, boots `HaloEmulator` (with an interactive 2× Pygame circular HUD window), launches PebbleOS in Playwright Chromium, streams dirty rects to Halo, and routes Halo clicks/taps to Pebble buttons. |
| [`test_encoder_pipeline.py`](./test_encoder_pipeline.py) | Fast standalone unit test verifying full-frame encoding, dirty-rectangle diffing (78-byte single-packet update), and bidirectional Halo button/tap callbacks without booting QEMU. |
| [`assets/`](./assets/) | Captured `256×256` Halo framebuffer screenshots from PebbleOS `gabbro`, sideloaded `.pbw` watchfaces, and `TouchyWeather v2.3.0`. |
| `brilliant_sdk/` *(gitignored)* | Cloned by `setup.sh` from `brilliantlabsAR/brilliant_sdk` (`halo-emulator 2.3.0`, `brilliant-msg 7.3.0`, `brilliant-ble 3.5.1`). |
| `pebble-qemu-wasm/` *(gitignored)* | Cloned by `setup.sh` from `ericmigi/pebble-qemu-wasm` (prebuilt QEMU WASM, PebbleOS firmware images, and `clang.wasm` + `lld.wasm`). |
| `TouchyWeather/` *(gitignored)* | Cloned by `setup.sh --with-touchyweather` from `ClickCalickClick/TouchyWeather`. |

---

## 4. Wire Protocol Specification (`PEBBLE_RECT_FLAG = 0x22`)

Each display update sent via `frame.send_message(0x22, payload)` uses a compact **10-byte big-endian header** followed by the custom RGB palette and packed pixel data:

| Byte Offset | Type | Field | Description |
|-------------|------|-------|-------------|
| `0` | `uint8` | `x` | 1-based Halo X coordinate (`1..256`) |
| `1` | `uint8` | `y` | 1-based Halo Y coordinate (`1..256`) |
| `2..3` | `uint16 BE` | `width` | Width of the rectangle in pixels (`1..256`) |
| `4..5` | `uint16 BE` | `height` | Height of the rectangle in pixels (`1..256`) |
| `6` | `uint8` | `flags` | `bit 0 (0x01)` = LZ4 compressed; `bit 1 (0x02)` = clear rect to `0x000000` before drawing |
| `7` | `uint8` | `bpp` | Bits per pixel: `1` (2 colors), `2` (4 colors), or `4` (16 colors) |
| `8` | `uint8` | `palette_offset` | Palette offset (`0..15`, default `0`) |
| `9` | `uint8` | `num_colors` | Number of RGB triplets in `palette_data` (`2`, `4`, or `16`) |
| `10 .. 10 + 3*N - 1` | `bytes` | `palette_data` | `num_colors × 3` bytes of RGB888 palette entries (Entry `0` is `(0,0,0)` transparent/black) |
| `10 + 3*N .. end` | `bytes` | `pixel_data` | Bit-packed pixel indices (`1bpp`, `2bpp`, or `4bpp`), either raw or LZ4-frame compressed |

**Why `clear_rect` + Index `0` Black Works So Well:**
In Halo's firmware (`modules/halo/src/lua_display.c`), palette index `0` (`VOID`) is always treated as **transparent** by `frame.display.bitmap()`. By setting `clear_rect = true`, `pebble_halo_app.lua` first fills the dirty rectangle `(x, y, w, h)` with black (`0x000000`) using `frame.display.rect(x, y, w, h, 0x000000, true)`, and then draws all non-black pixels (indices `1..15`) on top in a single `frame.display.bitmap()` call.

---

## 5. Input Mapping (Halo Glasses → Pebble Watch)

Pebble watches have 4 physical buttons (`Back`, `Up`, `Select`, `Down`). Halo has 1 physical button (supporting single, double, and long press) plus an BMA580 IMU temple-tap detector (supporting single, double, and triple tap):

| Halo Hardware Input | Pygame Emulator Key | BLE Code Sent by Halo | Mapped Pebble Button |
|---------------------|---------------------|-----------------------|----------------------|
| **Button Single-Click** | `SPACE` | `0x0B, 0x01` | **SELECT** |
| **Button Double-Click** | `D` | `0x0B, 0x02` | **BACK** |
| **Button Long-Press** | `L` | `0x0B, 0x03` | **BACK** |
| **IMU Temple Single-Tap** | `T` | `0x09, 0x01` | **DOWN** (Next card / menu item) |
| **IMU Temple Double-Tap** | `U` | `0x09, 0x02` | **UP** (Previous card / menu item) |
| *(Direct Keyboard Override)* | `Arrow Keys` / `Enter` / `Esc` | *(Direct to QEMU)* | **UP / DOWN / SELECT / BACK** |

---

## 6. How to Run the Demos

All dependencies (`halo-emulator`, `brilliant-msg`, `brilliant-ble`, `playwright`, `pygame-ce`, `lz4`, `pillow`, `numpy`) are managed by `uv` in [`pyproject.toml`](./pyproject.toml).

### A. Quick Encoder & Lua Unit Test (No QEMU boot, runs in ~2 seconds)
```bash
uv run python test_encoder_pipeline.py
```

### B. Interactive Live Mode — Sideload Any `.pbw` Watchface or App
Opens the 2× scaled circular **Halo Glasses HUD** Pygame window alongside the companion **PebbleOS Browser Window** (where you can paste any `apps.repebble.com` URL or drag-and-drop any `.pbw` file):
```bash
uv run python pebble_halo_bridge.py \
  --board gabbro \
  --pbw pebble-qemu-wasm/web/test-app.pbw \
  --show-browser
```

### C. Interactive Live Mode — Compile & Run `TouchyWeather` from C Source
Compiles `TouchyWeather` (`25+` C source files + `@rebble/clay` PebbleKit JS) in-browser using `clang.wasm` (`-Oz`) for `gabbro` (`260×260` circular), fetches live San Francisco weather from Open-Meteo, and streams it to the Halo HUD window:
```bash
uv run python pebble_halo_bridge.py \
  --board gabbro \
  --port 8095 \
  --build-zip http://127.0.0.1:8095/TouchyWeather.zip \
  --show-browser
```

### D. Automated Headless Capture Mode
Boots PebbleOS, compiles/installs the app, simulates Halo button clicks and temple taps, and saves `256×256` Halo framebuffer PNG snapshots:
```bash
uv run python pebble_halo_bridge.py \
  --demo-headless \
  --board gabbro \
  --port 8096 \
  --build-zip http://127.0.0.1:8096/TouchyWeather.zip
```

---

## 7. Captured Halo `256×256` Framebuffer Outputs

All images below were captured directly from `HaloEmulator.get_framebuffer()` after streaming over `EmulatorBrilliantMsg` (`0x22` packets) and rendering inside `lua/pebble_halo_app.lua`:

* **PebbleOS Circular Launcher Menu (`gabbro`):**
  * [`assets/halo_pebble_gabbro_01_watchface.png`](./assets/halo_pebble_gabbro_01_watchface.png) — Settings / Music / Notifications menu
  * [`assets/halo_pebble_gabbro_04_menu_scrolled.png`](./assets/halo_pebble_gabbro_04_menu_scrolled.png) — Scrolled down via simulated Halo temple tap
* **Sideloaded `.pbw` Binary (*"For Your Friends"* Analog Watchface):**
  * [`assets/halo_pebble_gabbro_02_installed_app.png`](./assets/halo_pebble_gabbro_02_installed_app.png) — Analog watchface with red second hand & date window
* **`TouchyWeather v2.3.0` Compiled from C + PebbleKit JS (Live Open-Meteo Forecast):**
  * [`assets/halo_pebble_gabbro_02_built_app.png`](./assets/halo_pebble_gabbro_02_built_app.png) — *"New on the horizon v2.3.0"* modal with animated rotating sun
  * [`assets/halo_pebble_gabbro_03_card.png`](./assets/halo_pebble_gabbro_03_card.png) — **Card 0 (Main Weather):** `53°`, `FEELS 50°`, `High 53° / Low 46°`, `4MPH NE`, `77%`
  * [`assets/halo_pebble_gabbro_04_card.png`](./assets/halo_pebble_gabbro_04_card.png) — **Card 1 (Touch & Go Advice):** *"COLD • FEELS LIKE 50° — Wind chill bites. Two pairs of socks is wisdom."*
  * [`assets/halo_pebble_gabbro_05_card.png`](./assets/halo_pebble_gabbro_05_card.png) — **Card 2 (6-Hour Forecast):** `4 PM – 9 PM` hourly conditions, temps, and wind direction arrows
  * [`assets/halo_pebble_gabbro_06_card.png`](./assets/halo_pebble_gabbro_06_card.png) — **Card 3 (Week Ahead):** `SUN – THU` 5-day forecast with rain probability badges (`94%`, `87%`)
  * [`assets/halo_pebble_gabbro_07_card.png`](./assets/halo_pebble_gabbro_07_card.png) — **Card 4 (Precipitation):** `Now` to `+4h` precipitation bars

---

## 8. Important Technical Notes & Fixes Discovered During PoC

1. **Multi-Block LZ4 Decompression in `pebble_halo_app.lua`:**
   `frame.compression.decompress(data, block_size)` invokes its `process_function` callback once per LZ4 block. In `HaloEmulator`, `decompress()` slices the decompressed output into `block_size` chunks, whereas on real firmware (`modules/halo/src/lua_compression.c`), `LZ4_decompress_safe` fires the callback once per LZ4 frame block (typically 64 KB). Our [`lua/pebble_halo_app.lua`](./lua/pebble_halo_app.lua#L91-L103) accumulates chunks in a table until `acc_len >= raw_bytes` before calling `frame.display.bitmap()`, guaranteeing identical behavior on both `HaloEmulator` and physical Halo glasses.
2. **In-Browser `clang.wasm` Size Optimization (`-Oz`) for Large Pebble Apps:**
   Pebble apps have a strict 64 KB (`65,536` bytes) binary + relocation table limit. Under `clang -Os`, `TouchyWeather` compiled to `65,632` bytes (96 bytes over the limit). Changing `-Os` to `-Oz` in [`pebble-qemu-wasm/appbuild/appbuilder.js`](./pebble-qemu-wasm/appbuild/appbuilder.js#L31) brought the binary comfortably under the 64 KB ceiling.
3. **Switching from `HaloEmulator` to Physical Halo Glasses:**
   Because [`pebble_halo_bridge.py`](./pebble_halo_bridge.py) uses `EmulatorBrilliantMsg` (which shares the exact same async API as `brilliant_msg.BrilliantMsg`), connecting to real Halo glasses over BLE only requires replacing:
   ```python
   emu = HaloEmulator()
   frame = EmulatorBrilliantMsg(emu)
   ```
   with:
   ```python
   from brilliant_msg import BrilliantMsg
   frame = BrilliantMsg()
   ```
4. **Next Step — Wrapping into a Flutter Android Companion App:**
   * Embed `pebble-qemu-wasm` inside a `flutter_inappwebview` widget in a Flutter app.
   * Use a JavaScript channel to post dirty-rect framebuffers from the WebView `<canvas>` to Dart.
   * Use the Flutter `brilliant_sdk` ([`brilliant_sdk/flutter`](./brilliant_sdk/flutter)) to upload `pebble_halo_app.lua` and stream the `0x22` dirty-rect packets over BLE to the paired Halo glasses.
