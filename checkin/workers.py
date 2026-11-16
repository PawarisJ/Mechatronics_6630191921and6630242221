"""Camera frame plumbing: the MJPEG output for browsers, the small frames for the vision workers,
and the loop that feeds a helper process and restarts it if it dies."""
import io
import json
import logging
import struct
import subprocess
import time
from threading import Condition

from . import config as C


class StreamingOutput(io.BufferedIOBase):
    """Receives each encoded JPEG frame from the camera; browsers wait on `condition` for the next one."""

    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


class FrameSource:
    """One thread grabs the small (lores) frames; each vision worker takes the newest one."""

    def __init__(self, capture):
        self.capture = capture  # returns an I420 array, shape (h*3/2, stride)
        self.cond = Condition()
        self.frame = None
        self.seq = 0

    def run(self):
        w, h = C.DETECT_SIZE
        while True:
            yuv = self.capture()
            with self.cond:
                self.frame = yuv[: h * 3 // 2, :w]
                self.seq += 1
                self.cond.notify_all()

    def wait_new(self, last_seq):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq)
            return self.frame, self.seq


def worker_loop(frames, script, on_result, on_down, set_alive, active, max_fps=None, header_extra=None):
    """Feed small camera frames to a helper process (at most max_fps) while active() is true;
    a paused worker just waits on its input and uses no CPU. Restart it if it dies."""
    w, h = C.DETECT_SIZE
    header = struct.pack("<II", w, h)
    while True:
        proc = subprocess.Popen([C.WORKER_PYTHON, script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=False)
        set_alive(True)
        last, seq, fps = time.monotonic(), 0, 0.0
        try:
            while True:
                if not active():
                    time.sleep(0.05)
                    last = time.monotonic()
                    continue
                cap = max_fps() if callable(max_fps) else max_fps
                if cap:
                    time.sleep(max(0.0, last + 1.0 / cap - time.monotonic()))
                yuv, seq = frames.wait_new(seq)
                proc.stdin.write(header + (header_extra() if header_extra else b"") + yuv.tobytes())
                proc.stdin.flush()
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("worker exited")
                now = time.monotonic()
                fps = 0.8 * fps + 0.2 / max(now - last, 1e-3)
                last = now
                on_result(json.loads(line), fps)
        except Exception as e:
            logging.warning("%s problem: %s; restarting in 2 s", script, e)
            proc.kill()
            set_alive(False)
            on_down()
            time.sleep(2)
