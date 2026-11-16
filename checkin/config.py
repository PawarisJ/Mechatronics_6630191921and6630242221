"""All settings for the check-in system: paths, camera, recognition, blink, finger code and timings."""
import os
import re

HOME = os.path.expanduser("~")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # folder with camera_stream.py

# ---- web + camera
PORT = 8000
RESOLUTION = (1280, 720)     # browser video (~12.7 Mbit/s per viewer at 30 fps)
CAMERA_FPS = 30
STREAM_BITRATE = None        # None = encoder default quality (960x540/15 fps/3 Mbit/s was tried, user preferred this)
STREAM_SEND_BUFFER = 64 * 1024  # bytes per viewer socket (Linux doubles it ~= 2 frames): stops delay building up
DETECT_SIZE = (640, 360)     # small frames sent to the vision workers, for speed
USER_PAGE_FILE = os.path.join(BASE_DIR, "user_page.html")    # pages are re-read on every request,
ADMIN_PAGE_FILE = os.path.join(BASE_DIR, "admin_page.html")  # so they can change without a restart

# ---- helper processes (Python 3.12 venv with MediaPipe + OpenCV)
WORKER_PYTHON = os.path.join(HOME, "mp312/bin/python")
FACE_WORKER = os.path.join(BASE_DIR, "face_worker.py")
HAND_WORKER = os.path.join(BASE_DIR, "gesture_worker.py")
FACE_MAX_FPS = 10            # face recognition rate cap (the blink step is not capped)

# ---- files
WEBHOOK_FILE = os.path.join(HOME, "sheets_webhook_url.txt")    # Google Apps Script web-app URL
ADMIN_PASSWORD_FILE = os.path.join(HOME, "admin_password.txt")  # if present: admin page needs a login
FACES_DIR = os.path.join(HOME, "faces")
PEOPLE_FILE = os.path.join(FACES_DIR, "people.json")
ACCESS_CODE_FILE = os.path.join(FACES_DIR, "access_code.json")  # shared code, salted + hashed
PHOTOS_DIR = os.path.join(FACES_DIR, "photos")
ATTENDANCE_CSV = os.path.join(HOME, "attendance_log.csv")  # check-ins / resets (used to rebuild today's list)
ACCESS_CSV = os.path.join(HOME, "access_log.csv")          # every access attempt (same as the Google Sheet)
ACCESS_HEADER = ["Date", "Time", "Employee ID", "Name", "Face match (%)", "Blink check", "Password", "Result"]
RESET_STATUS = "Reset by admin"

# ---- debug logs of raw finger / blink measurements (for re-tuning thresholds); off in normal use
DEBUG_LOGS = False
FINGER_DEBUG_LOG = os.path.join(HOME, "finger_debug.log")
BLINK_DEBUG_LOG = os.path.join(HOME, "blink_debug.log")

# ---- face recognition + registration
MATCH_THRESHOLD = 0.40       # SFace cosine similarity needed to accept a match (OpenCV suggests 0.363)
CONFIRM_HITS = 2             # this many embeddings in a row must agree before a name is accepted
DUPLICATE_THRESHOLD = 0.50   # refuse to register a face this similar to a different registered person
REGISTER_SAMPLES = 5
REGISTER_INTERVAL = 0.4      # seconds between registration samples
REGISTER_TIMEOUT = 40

# ---- liveness: a blink is the eye-closed score jumping above the person's OWN open-eye level and
# dropping back. Tuned on logged data: open-eye levels differ per person (0.1-0.3, e.g. glasses) and
# real blinks often peak at only 0.4-0.45 for one frame; a photo stays flat (noise under ~0.1).
BLINK_TIMEOUT = 8
BLINK_RISE = 0.16            # closed = average eye score at least this far above the open-eye level
BLINK_MIN_CLOSED = 0.30      # ...and at least this high
BLINK_RECOVER = 0.07         # open again = back within this of the open-eye level
BLINK_BASELINE_FRAMES = 8    # open-eye level = median of the last N frames
BLINK_BASELINE_MIN = 3       # frames needed before a blink can count
TRACK_LOST_SECONDS = 1.0     # face out of view this long during the blink step -> start over

# ---- finger code
CODE_LENGTH = 3
CODE_RE = re.compile(r"^[0-5]{3}$")  # digits 0-5: one hand (0 = fist, 1-5 = raised fingers)
CODE_TIMEOUT = 15            # seconds allowed for each digit
HOLD_SECONDS = 1.0           # a finger count must stay the same this long to be accepted
REPEAT_HOLD_SECONDS = 2.0    # the same digit as the one just entered needs a longer hold
CHANGE_PAUSE = 1.5           # after each digit: "change number" pop-up, hand input ignored
RETRY_PAUSE = 2.0            # after a wrong code: "wrong code" pop-up before starting again
MAX_ATTEMPTS = 3             # wrong codes allowed before access is denied
LOCKOUT_SECONDS = 60         # after MAX_ATTEMPTS wrong codes, that person is ignored this long

# ---- result screen
RESULT_SECONDS = 4
COOLDOWN_SECONDS = 30        # after a result, ignore that person this long (they are still in view)
RETRY_COOLDOWN = 2           # after a timeout, the person can retry this soon

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
