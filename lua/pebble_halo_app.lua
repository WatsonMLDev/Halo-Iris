-- pebble_halo_app.lua
-- Halo receiver app for PebbleOS display frames & dirty rectangles.
-- Also forwards Halo physical button clicks (0x0B) and IMU temple taps (0x09)
-- back to the host bridge so they map to Pebble watch buttons.
-- NOTE: Keep ASCII-only for HaloEmulator latin-1 compatibility.

local data = require('data.min')

-- Host -> Halo message flags
local PEBBLE_RECT_FLAG = 0x22
local CLEAR_FLAG = 0x10

-- Halo -> Host message flags
local TAP_FLAG = 0x09
local CLICK_FLAG = 0x0B

local TAP_CODE = { single = 1, double = 2, triple = 3 }

-- Wake up display on Halo hardware
if frame.HARDWARE_VERSION ~= 'Frame' then
    frame.display.power_save(false)
end

-- Register Halo button callbacks -> send to host
frame.button.single(function()
    pcall(frame.bluetooth.send, string.char(CLICK_FLAG, 1))
end)

frame.button.double(function()
    pcall(frame.bluetooth.send, string.char(CLICK_FLAG, 2))
end)

frame.button.long(function()
    pcall(frame.bluetooth.send, string.char(CLICK_FLAG, 3))
end)

-- Register Halo IMU tap callback -> send to host
frame.imu.tap_callback(function(kind)
    local n = TAP_CODE[kind] or 1
    pcall(frame.bluetooth.send, string.char(TAP_FLAG, n))
end)

-- Initial splash screen while waiting for PebbleOS frame
frame.display.clear(0x000000)
frame.display.set_font(1, 16, 1)
frame.display.text("PEBBLE x HALO", 32, 105, 0x4CC9F0)
frame.display.set_font(0, 8, 1)
frame.display.text("Waiting for PebbleOS...", 44, 135, 0xAAAAAA)

-- Signal host that Lua app is ready (await_print)
print("Pebble Halo App Ready")

local function draw_pebble_rect(raw)
    -- Header layout (10 bytes):
    -- [1]: x (1..256)
    -- [2]: y (1..256)
    -- [3..4]: width (uint16 big-endian)
    -- [5..6]: height (uint16 big-endian)
    -- [7]: flags (bit0 = compressed, bit1 = clear_rect)
    -- [8]: bpp (1, 2, or 4)
    -- [9]: palette_offset (0..15)
    -- [10]: num_colors (0..16)
    -- [11 .. 10 + num_colors*3]: palette_data (RGB triplets)
    -- [11 + num_colors*3 .. end]: pixel_data (raw or LZ4 frame)
    local x = string.byte(raw, 1)
    local y = string.byte(raw, 2)
    local w = (string.byte(raw, 3) << 8) | string.byte(raw, 4)
    local h = (string.byte(raw, 5) << 8) | string.byte(raw, 6)
    local flags = string.byte(raw, 7)
    local bpp = string.byte(raw, 8)
    local pal_offset = string.byte(raw, 9)
    local num_colors = string.byte(raw, 10)

    local compressed = (flags & 1) ~= 0
    local clear_rect = (flags & 2) ~= 0

    local pal_end = 10 + num_colors * 3
    local palette_data = string.sub(raw, 11, pal_end)
    local pixel_data = string.sub(raw, pal_end + 1)

    local color_format = 1 << bpp
    local opts = nil
    if num_colors > 0 then
        opts = { palette_data = palette_data }
    end

    if clear_rect then
        frame.display.rect(x, y, w, h, 0x000000, true)
    end

    if compressed then
        local ppb = 8 // bpp
        local raw_bytes = (w * h + ppb - 1) // ppb
        local acc = {}
        local acc_len = 0
        frame.compression.process_function(function(chunk)
            acc[#acc + 1] = chunk
            acc_len = acc_len + #chunk
            if acc_len >= raw_bytes then
                local full_data = table.concat(acc)
                frame.display.bitmap(x, y, w, color_format, pal_offset, full_data, opts)
            end
        end)
        frame.compression.decompress(pixel_data, raw_bytes)
    else
        frame.display.bitmap(x, y, w, color_format, pal_offset, pixel_data, opts)
    end
end

while true do
    local rc, err = pcall(function()
        local items = data.process_raw_items()
        for i = 1, #items do
            local flag = items[i][1]
            local raw = items[i][2]
            if flag == PEBBLE_RECT_FLAG then
                draw_pebble_rect(raw)
            elseif flag == CLEAR_FLAG then
                frame.display.clear(0x000000)
            end
        end
        frame.sleep(0.005)
    end)
    if rc == false then
        print("Lua error: " .. tostring(err))
        frame.display.clear(0x000000)
        break
    end
end
