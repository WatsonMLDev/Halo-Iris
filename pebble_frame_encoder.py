"""
pebble_frame_encoder.py — Frame diffing, circular masking, color quantization,
and LZ4 packing from PebbleOS framebuffer (260x260 Gabbro or 180x180 Chalk)
to Brilliant Labs Halo (256x256 circular display).
"""
from __future__ import annotations

import struct
import lz4.frame
import numpy as np
from PIL import Image

HALO_SIZE = 256
PEBBLE_RECT_FLAG = 0x22


def _build_circular_mask(size: int = HALO_SIZE) -> np.ndarray:
    """Boolean (H, W) mask where True = inside the 256x256 circular lens."""
    y, x = np.ogrid[:size, :size]
    cx, cy = (size - 1) / 2.0, (size - 1) / 2.0
    r = size / 2.0
    return ((x - cx) ** 2 + (y - cy) ** 2) <= (r ** 2)


_CIRCLE_MASK_256 = _build_circular_mask(HALO_SIZE)


def normalize_pebble_frame(img: Image.Image, scale_chalk: bool = False) -> Image.Image:
    """
    Convert a Pebble framebuffer PIL Image into a 256x256 RGB image for Halo:
      - 260x260 (Gabbro / Pebble Round 2): center-crop 2px off each edge -> 256x256
      - 180x180 (Chalk / Pebble Time Round): either center in 256x256 (38px margin)
        or nearest-neighbor scale to 256x256 if scale_chalk=True
      - 144x168 / 200x228 (Rectangular Pebbles): center inside 256x256
    Pixels outside the 256x256 circular lens are masked to black (0, 0, 0) so
    we never waste palette slots or BLE bytes on invisible corner pixels.
    """
    rgb = img.convert("RGB")
    w, h = rgb.size

    if w == 260 and h == 260:
        canvas = rgb.crop((2, 2, 258, 258))
    elif w == HALO_SIZE and h == HALO_SIZE:
        canvas = rgb
    elif scale_chalk and w == 180 and h == 180:
        canvas = rgb.resize((HALO_SIZE, HALO_SIZE), Image.Resampling.NEAREST)
    else:
        canvas = Image.new("RGB", (HALO_SIZE, HALO_SIZE), (0, 0, 0))
        ox = (HALO_SIZE - w) // 2
        oy = (HALO_SIZE - h) // 2
        canvas.paste(rgb, (ox, oy))

    arr = np.array(canvas, dtype=np.uint8)
    arr[~_CIRCLE_MASK_256] = 0
    return Image.fromarray(arr, mode="RGB")


def compute_dirty_bbox(
    prev_rgb: np.ndarray | None,
    curr_rgb: np.ndarray,
) -> tuple[int, int, int, int] | None:
    """
    Compare previous and current (256, 256, 3) uint8 arrays.
    Returns (x0, y0, w, h) in 0-based coordinates, or None if identical.
    """
    if prev_rgb is None:
        return (0, 0, HALO_SIZE, HALO_SIZE)

    diff_mask = np.any(prev_rgb != curr_rgb, axis=2)
    if not np.any(diff_mask):
        return None

    ys, xs = np.where(diff_mask)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    return (x0, y0, x1 - x0 + 1, y1 - y0 + 1)


def _pack_1bit(indices: np.ndarray) -> bytes:
    return np.packbits(indices.astype(np.uint8)).tobytes()


def _pack_2bit(indices: np.ndarray) -> bytes:
    flat = indices.astype(np.uint8).ravel()
    pad = (-len(flat)) % 4
    if pad:
        flat = np.concatenate([flat, np.zeros(pad, dtype=np.uint8)])
    reshaped = flat.reshape(-1, 4)
    packed = (
        ((reshaped[:, 0] & 0x3) << 6)
        | ((reshaped[:, 1] & 0x3) << 4)
        | ((reshaped[:, 2] & 0x3) << 2)
        | (reshaped[:, 3] & 0x3)
    )
    return packed.astype(np.uint8).tobytes()


def _pack_4bit(indices: np.ndarray) -> bytes:
    flat = indices.astype(np.uint8).ravel()
    pad = (-len(flat)) % 2
    if pad:
        flat = np.concatenate([flat, np.zeros(pad, dtype=np.uint8)])
    reshaped = flat.reshape(-1, 2)
    packed = ((reshaped[:, 0] & 0xF) << 4) | (reshaped[:, 1] & 0xF)
    return packed.astype(np.uint8).tobytes()


def encode_pebble_rect(
    curr_rgb: np.ndarray,
    bbox: tuple[int, int, int, int],
    *,
    compress: bool = True,
    clear_rect: bool = True,
) -> tuple[bytes, dict]:
    """
    Encode a dirty rectangle (x0, y0, w, h) from curr_rgb into a PEBBLE_RECT_FLAG
    binary payload ready for `frame.send_message(0x22, payload)`.

    Uses index 0 for pure black (0, 0, 0) and assigns non-black colors to
    indices 1..15 (up to 15 non-black colors + black = 16 total colors).
    Automatically picks 1bpp, 2bpp, or 4bpp depending on unique colors in the rect!
    """
    x0, y0, w, h = bbox
    sub = curr_rgb[y0 : y0 + h, x0 : x0 + w]

    # Identify black pixels (0, 0, 0) -> these stay at index 0 (transparent after clear_rect)
    is_black = (sub[:, :, 0] == 0) & (sub[:, :, 1] == 0) & (sub[:, :, 2] == 0)

    # Quantize non-black colors to at most 15 colors so index 0 is reserved for black
    sub_img = Image.fromarray(sub, mode="RGB")
    quant = sub_img.quantize(colors=15, method=Image.Quantize.FASTOCTREE)
    raw_pal = quant.getpalette() or [0, 0, 0]
    q_indices = np.array(quant, dtype=np.uint8)

    # Shift quantized indices by +1 (1..15), and set black pixels to 0
    shifted_indices = q_indices + 1
    shifted_indices[is_black] = 0

    max_idx = int(shifted_indices.max()) if shifted_indices.size > 0 else 0
    if max_idx <= 1:
        bpp = 1
        num_colors = 2
        pack_fn = _pack_1bit
    elif max_idx <= 3:
        bpp = 2
        num_colors = 4
        pack_fn = _pack_2bit
    else:
        bpp = 4
        num_colors = 16
        pack_fn = _pack_4bit

    # Build palette bytes: entry 0 = (0,0,0), entries 1..(num_colors-1) from quant palette
    pal_bytes = bytearray(num_colors * 3)
    for i in range(1, num_colors):
        src_off = (i - 1) * 3
        if src_off + 3 <= len(raw_pal):
            pal_bytes[i * 3 : i * 3 + 3] = bytes(raw_pal[src_off : src_off + 3])

    packed_pixels = pack_fn(shifted_indices)
    raw_pixel_len = len(packed_pixels)

    use_lz4 = False
    if compress and raw_pixel_len > 64:
        compressed_pixels = lz4.frame.compress(packed_pixels, compression_level=9)
        if len(compressed_pixels) < raw_pixel_len:
            packed_pixels = compressed_pixels
            use_lz4 = True

    flags = (1 if use_lz4 else 0) | (2 if clear_rect else 0)
    # Halo coordinates are 1-based (1..256)
    hx = x0 + 1
    hy = y0 + 1

    header = struct.pack(
        ">BBHHBBBB",
        hx,
        hy,
        w,
        h,
        flags,
        bpp,
        0,  # palette_offset
        num_colors,
    )
    payload = header + bytes(pal_bytes) + packed_pixels
    stats = {
        "bbox": (hx, hy, w, h),
        "bpp": bpp,
        "num_colors": num_colors,
        "raw_bytes": raw_pixel_len,
        "payload_bytes": len(payload),
        "compressed": use_lz4,
    }
    return payload, stats
