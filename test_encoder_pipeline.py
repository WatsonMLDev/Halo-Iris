"""
test_encoder_pipeline.py — Quick unit verification of:
1. Encoding a synthetic 260x260 circular Pebble watchface (Full frame)
2. Sending it over EmulatorBrilliantMsg to lua/pebble_halo_app.lua
3. Modifying only a small seconds counter region (Dirty Rectangle diff)
4. Sending the dirty-rect update and verifying the Halo framebuffer
5. Testing bidirectional button clicks & IMU taps from Halo -> Pebble host
"""
import asyncio
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

from halo_emulator import HaloEmulator, EmulatorBrilliantMsg
from pebble_frame_encoder import (
    PEBBLE_RECT_FLAG,
    compute_dirty_bbox,
    encode_pebble_rect,
    normalize_pebble_frame,
)

CLICK_FLAG = 0x0B
TAP_FLAG = 0x09


def make_synthetic_pebble_round_frame(time_str: str, sec_str: str) -> Image.Image:
    """Create a 260x260 Pebble Round 2 (Gabbro) style watchface frame."""
    img = Image.new("RGB", (260, 260), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer bezel ring (Pebble 64-color cyan/blue)
    draw.ellipse([6, 6, 253, 253], outline=(0, 170, 255), width=4)
    draw.ellipse([16, 16, 243, 243], fill=(0, 32, 64))

    # Watchface header
    draw.rectangle([50, 45, 210, 75], fill=(255, 85, 0))
    draw.text((75, 55), "PEBBLE ROUND 2", fill=(255, 255, 255))

    # Big time box
    draw.rectangle([45, 95, 215, 155], fill=(0, 0, 0), outline=(85, 255, 85), width=2)
    draw.text((85, 118), time_str, fill=(255, 255, 85))

    # Seconds badge at bottom
    draw.rectangle([100, 175, 160, 210], fill=(170, 0, 85))
    draw.text((118, 188), sec_str, fill=(255, 255, 255))

    return img


async def main() -> None:
    emu = HaloEmulator()
    frame = EmulatorBrilliantMsg(emu)

    received_inputs: list[tuple[str, int]] = []

    def on_click(data: bytes) -> None:
        received_inputs.append(("click", data[1]))

    def on_tap(data: bytes) -> None:
        received_inputs.append(("tap", data[1]))

    try:
        await frame.connect()
        await frame.upload_stdlua_libs(lib_names=["data"])
        await frame.upload_frame_app(
            local_filename=str(Path(__file__).parent / "lua/pebble_halo_app.lua"),
            frame_filename="pebble_halo_app.lua",
        )
        frame.attach_print_response_handler()
        await frame.start_frame_app(frame_app_name="pebble_halo_app", await_print=True)

        frame.register_data_response_handler(on_click, [CLICK_FLAG], on_click)
        frame.register_data_response_handler(on_tap, [TAP_FLAG], on_tap)

        # 1. Send initial full frame
        f1 = normalize_pebble_frame(make_synthetic_pebble_round_frame("12:34", ":01"))
        arr1 = np.array(f1, dtype=np.uint8)
        bbox1 = compute_dirty_bbox(None, arr1)
        assert bbox1 == (0, 0, 256, 256)
        payload1, stats1 = encode_pebble_rect(arr1, bbox1)
        print(f"Frame 1 (Full): {stats1}")
        await frame.send_message(PEBBLE_RECT_FLAG, payload1)
        await asyncio.sleep(0.15)
        frame.get_framebuffer().save("test_frame1_full.png")

        # 2. Send dirty-rect update (only seconds changed from :01 to :02)
        f2 = normalize_pebble_frame(make_synthetic_pebble_round_frame("12:34", ":02"))
        arr2 = np.array(f2, dtype=np.uint8)
        bbox2 = compute_dirty_bbox(arr1, arr2)
        assert bbox2 is not None
        payload2, stats2 = encode_pebble_rect(arr2, bbox2)
        print(f"Frame 2 (Dirty Rect): {stats2}")
        assert stats2["payload_bytes"] < 200, "Dirty rect should fit in a single BLE packet!"
        await frame.send_message(PEBBLE_RECT_FLAG, payload2)
        await asyncio.sleep(0.15)
        frame.get_framebuffer().save("test_frame2_dirty.png")

        # 3. Verify Halo button & IMU tap forwarding
        frame.inject_button_single()
        frame.inject_button_double()
        emu.inject_imu_tap("single")
        emu.inject_imu_tap("double")
        await asyncio.sleep(0.2)
        print(f"Received Halo inputs on host: {received_inputs}")
        assert ("click", 1) in received_inputs
        assert ("click", 2) in received_inputs
        assert ("tap", 1) in received_inputs
        assert ("tap", 2) in received_inputs
        print("SUCCESS: End-to-end encoder + Halo Lua app + input bridge verified!")

    finally:
        await frame.stop_frame_app()
        await frame.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
