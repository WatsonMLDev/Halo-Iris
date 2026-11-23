#!/usr/bin/env bash
# setup.sh — One-command bootstrap for PebbleOS x Brilliant Labs Halo Bridge
# Clones external dependencies (brilliant_sdk, pebble-qemu-wasm, and optionally TouchyWeather),
# applies the -Oz size-optimization patch for clang.wasm, and sets up the Python virtual environment.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

echo "=== [1/4] Cloning external repositories ==="
if [ ! -d "brilliant_sdk" ]; then
    git clone https://github.com/brilliantlabsAR/brilliant_sdk.git
else
    echo "  -> brilliant_sdk already exists, skipping clone."
fi

if [ ! -d "pebble-qemu-wasm" ]; then
    git clone https://github.com/ericmigi/pebble-qemu-wasm.git
else
    echo "  -> pebble-qemu-wasm already exists, skipping clone."
fi

# Apply the -Oz optimization tweak in pebble-qemu-wasm so large 64KB apps (like TouchyWeather)
# fit inside Pebble's 65,536-byte app binary + relocation table limit, and only build the active board.
if [ -f "pebble-qemu-wasm/appbuild/appbuilder.js" ]; then
    sed -i "s/'-Os'/'-Oz'/g" pebble-qemu-wasm/appbuild/appbuilder.js
fi
if [ -f "pebble-qemu-wasm/index.html" ]; then
    sed -i "s/alsoPlatforms: \['emery', 'flint', 'gabbro', 'aplite', 'basalt', 'chalk', 'diorite'\]/alsoPlatforms: []/g" pebble-qemu-wasm/index.html
fi

echo "=== [2/4] Setting up Python virtual environment (.venv) ==="
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install \
    -e ./brilliant_sdk/python/packages/brilliant_ble \
    -e ./brilliant_sdk/python/packages/brilliant_msg \
    -e ./brilliant_sdk/python/packages/brilliant_sdk \
    -e ./brilliant_sdk/python/packages/halo_emulator \
    -r requirements.txt

echo "=== [3/4] Checking Playwright Chromium ==="
if [ ! -x "/usr/bin/chromium" ]; then
    echo "  -> System /usr/bin/chromium not found; installing Playwright Chromium..."
    ./.venv/bin/playwright install chromium
else
    echo "  -> Found system /usr/bin/chromium."
fi

echo "=== [4/4] (Optional) Preparing TouchyWeather sample app ==="
if [ "${1:-}" = "--with-touchyweather" ] || [ -d "TouchyWeather" ]; then
    if [ ! -d "TouchyWeather" ]; then
        git clone https://github.com/ClickCalickClick/TouchyWeather.git
    fi
    if command -v npm >/dev/null 2>&1; then
        npm --prefix TouchyWeather install
    fi
    ./.venv/bin/python - << 'PYEOF'
import pathlib, re, shutil, zipfile

root = pathlib.Path("TouchyWeather")
secrets = root / "src/pkjs/secrets.js"
if not secrets.exists() and (root / "src/pkjs/secrets.js.example").exists():
    shutil.copy2(root / "src/pkjs/secrets.js.example", secrets)

text = (root / "CHANGELOG.md").read_text()
m = re.search(r"(?m)^##\s+(\d+)\.(\d+)\.(\d+)[^\n]*\n(.*?)(?=^##\s|\Z)", text, re.S)
if m:
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    code = major * 10000 + minor * 100 + patch
    label = f"{major}.{minor}.{patch}"
    bullets = [line.strip()[1:].strip() for line in m.group(4).splitlines() if line.strip().startswith("-")]
    notes = r"\n".join(bullets).replace('"', r'\"')
    (root / "src/c/version_gen.h").write_text(
        f'#pragma once\n#define APP_VERSION_CODE {code}\n#define APP_VERSION_LABEL "{label}"\n#define APP_UPDATE_NOTES "{notes}"\n'
    )

zip_path = pathlib.Path("pebble-qemu-wasm/TouchyWeather.zip")
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    for p in root.rglob("*"):
        if ".git" in p.parts or "screenshots" in p.parts:
            continue
        if p.is_file():
            zf.write(p, "TouchyWeather/" + str(p.relative_to(root)))
print(f"  -> Packed {zip_path} ({zip_path.stat().st_size} bytes)")
PYEOF
fi

echo ""
echo "Setup complete! Quick test commands:"
echo "  1. Fast encoder + Lua test:  ./.venv/bin/python test_encoder_pipeline.py"
echo "  2. Live interactive bridge:  ./.venv/bin/python pebble_halo_bridge.py --board gabbro --pbw pebble-qemu-wasm/web/test-app.pbw --show-browser"
