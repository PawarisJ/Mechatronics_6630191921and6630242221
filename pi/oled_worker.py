#!/usr/bin/env python3
"""OLED status screen (SSD1306 128x64 on I2C bus 1, address 0x3c), started by camera_stream.py.
Runs in the Python 3.12 venv ~/mp312 (luma.oled). It reads the kiosk's /status.json a few times a
second and redraws only when the picture changes. An OLED problem only stops this program: the kiosk
keeps running and camera_stream.py starts it again later.

Exit codes: 3 = display not found (I2C off / not wired); 0 = stopped (SIGTERM) or kiosk gone.
Preview without hardware:  python oled_worker.py --preview <folder>   (writes one PNG per screen)
"""
import datetime
import json
import os
import signal
import sys
import time
import urllib.request

STATUS_URL = "http://127.0.0.1:8000/status.json"
POLL_SECONDS = 0.25
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
WIDTH, HEIGHT = 128, 64

RESULT_WORDS = {"denied": "DENIED", "timeout": "TIME UP", "no_blink": "NO BLINK", "no_code": "NO CODE"}


def layout(status, now):
    """What to show, as a small dict (also used to detect changes):
    top / big / bottom text lines, invert = draw `big` white-on-black reversed, clock = big is a clock."""
    if status is None:
        return {"top": "", "big": "...", "bottom": "Starting", "invert": False, "clock": False}
    stage, result, prompt = status.get("stage"), status.get("result"), status.get("prompt") or {}
    if stage == "result" and result:
        name, kind = result.get("name", ""), result.get("kind")
        if kind in ("checked_in", "already"):
            timing = result.get("timing") or ""
            big = "LATE" if timing == "Late" else "ON TIME" if timing == "On time" else "CHECKED IN"
            when = (result.get("time") or "")[:5]
            bottom = when if kind == "checked_in" else "Already in " + when
            return {"top": name, "big": big, "bottom": bottom, "invert": timing == "Late", "clock": False}
        return {"top": name, "big": RESULT_WORDS.get(kind, "TRY AGAIN"), "bottom": "", "invert": True,
                "clock": False}
    if stage == "blink":
        return {"top": prompt.get("name", ""), "big": "BLINK", "bottom": "Blink slowly", "invert": False,
                "clock": False}
    if stage == "code":
        return {"top": prompt.get("name", ""), "big": "CODE %d/3" % (prompt.get("entered", 0) + 1),
                "bottom": "Show fingers", "invert": False, "clock": False}
    sched = status.get("schedule") or {}
    cutoff = sched.get("late_after", "")
    if not cutoff:
        bottom = "Look at camera"
    elif sched.get("late_now"):
        bottom = "Now: LATE (" + cutoff + ")"
    else:
        bottom = "On time until " + cutoff
    return {"top": "", "big": now.strftime("%H:%M"), "bottom": bottom, "invert": False, "clock": True}


def _fonts():
    from PIL import ImageFont
    bold = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
    # "big" is a list, largest first: the status word uses the largest size that fits the width
    return {"clock": ImageFont.truetype(bold, 34),
            "big": [ImageFont.truetype(bold, n) for n in (34, 30, 27, 24, 22, 18, 14)],
            "small": ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"), 12),
            "name": ImageFont.truetype(bold, 13)}


def _fit(draw, text, font, width):
    """Cut text (adding '…') until it fits in `width` pixels."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def _centre(draw, y, text, font, fill="white"):
    text = _fit(draw, text, font, WIDTH - 2)
    draw.text(((WIDTH - draw.textlength(text, font=font)) / 2, y), text, font=font, fill=fill)


def draw_screen(draw, spec, fonts):
    if spec["clock"]:
        _centre(draw, 2, spec["big"], fonts["clock"])
        _centre(draw, 46, spec["bottom"], fonts["small"])
        return
    _centre(draw, 0, spec["top"], fonts["name"])
    big_font = next((f for f in fonts["big"] if draw.textlength(spec["big"], font=f) <= WIDTH - 4), fonts["big"][-1])
    # vertically centre the word's ink in the band between the name (y<15) and the bottom line (y>=51)
    x0, y0, x1, y1 = draw.textbbox((0, 0), spec["big"], font=big_font)
    y = 15 + (36 - (y1 - y0)) / 2 - y0
    if spec["invert"]:
        draw.rectangle((0, 15, WIDTH - 1, 50), fill="white")
        _centre(draw, y, spec["big"], big_font, fill="black")
    else:
        _centre(draw, y, spec["big"], big_font)
    _centre(draw, 51, spec["bottom"], fonts["small"])


def fetch_status():
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=2) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError):
        return None


def preview(folder):
    from luma.core.device import dummy
    from luma.core.render import canvas
    os.makedirs(folder, exist_ok=True)
    now = datetime.datetime(2026, 11, 16, 7, 52)
    sched = {"late_after": "08:00", "late_now": False}
    cases = {
        "idle": {"stage": "face", "result": None, "schedule": sched},
        "idle_late": {"stage": "face", "result": None, "schedule": {"late_after": "08:00", "late_now": True}},
        "blink": {"stage": "blink", "prompt": {"name": "Moss"}},
        "code": {"stage": "code", "prompt": {"name": "Moss", "entered": 1}},
        "on_time": {"stage": "result", "result": {"kind": "checked_in", "name": "Moss", "time": "07:52:10",
                                                  "timing": "On time"}},
        "late": {"stage": "result", "result": {"kind": "checked_in", "name": "A Very Long Name Here",
                                               "time": "08:12:03", "timing": "Late"}},
        "already_old": {"stage": "result", "result": {"kind": "already", "name": "Moss", "time": "14:32:10",
                                                      "timing": ""}},
        "denied": {"stage": "result", "result": {"kind": "denied", "name": "Moss"}},
        "starting": None,
    }
    fonts = _fonts()
    for name, status in cases.items():
        dev = dummy(width=WIDTH, height=HEIGHT, mode="1")
        with canvas(dev) as draw:
            draw_screen(draw, layout(status, now), fonts)
        dev.image.resize((WIDTH * 4, HEIGHT * 4)).save(os.path.join(folder, name + ".png"))
    print("wrote", len(cases), "previews to", folder)


def main():
    from luma.core.interface.serial import i2c
    from luma.core.render import canvas
    from luma.oled.device import ssd1306
    try:
        device = ssd1306(i2c(port=1, address=0x3C))  # clears the screen on exit (persist=False)
    except Exception as e:
        print("OLED not available (%s): is I2C on and the display wired?" % e, flush=True)
        sys.exit(3)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # exit -> luma clears the display
    fonts = _fonts()
    parent = os.getppid()
    shown = None
    print("OLED running", flush=True)
    while os.getppid() == parent:
        spec = layout(fetch_status(), datetime.datetime.now())
        if spec != shown:
            with canvas(device) as draw:
                draw_screen(draw, spec, fonts)
            shown = spec
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--preview":
        preview(sys.argv[2])
    else:
        main()
