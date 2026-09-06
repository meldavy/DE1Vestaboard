#!/usr/bin/env python3
"""
DE1+ to Vestaboard MQTT Bridge

Subscribes to the DE1+ MQTT state topic and posts the current message
to Vestaboard when the machine transitions from sleep to wake.
"""

import json
import os
import sys
import time
import signal
import logging
import threading

import paho.mqtt.client as mqtt
import requests

# Configuration
MQTT_BROKER = os.environ.get("MQTT_BROKER", "eclipse-mosquitto_eclipse-mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_PREFIX = os.environ.get("MQTT_PREFIX", "de1")
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "state")

# Vestaboard transport: "cloud" (https://cloud.vestaboard.com) or "local"
# (the board's on-LAN local API at http://<host>:<port>).
VESTABOARD_API_MODE = os.environ.get("VESTABOARD_API_MODE", "cloud").strip().lower()
VESTABOARD_CLOUD_API_URL = "https://cloud.vestaboard.com/"
VESTABOARD_TOKEN = os.environ.get("VESTABOARD_TOKEN", "")  # cloud API token

# Local API: host defaults to the board's .local name. The API key must be
# passed via VESTABOARD_LOCAL_API_KEY (obtained with a one-time enablement
# call done outside this app). Local mode requires this key.
VESTABOARD_LOCAL_HOST = os.environ.get("VESTABOARD_LOCAL_HOST", "192.168.1.169")
VESTABOARD_LOCAL_PORT = int(os.environ.get("VESTABOARD_LOCAL_PORT", "7000"))
VESTABOARD_LOCAL_API_KEY = os.environ.get("VESTABOARD_LOCAL_API_KEY", "")

# Minimum seconds between Vestaboard posts. Observed via the local API that the
# board can physically update about once every 15s, so 16 keeps one step of
# headroom against drops.
POST_INTERVAL_SEC = float(os.environ.get("POST_INTERVAL_SEC", "16"))

# Path of the JSON file containing the personalized wake/startup layout.
# The file is re-read on every wake transition, so edits take effect without
# restarting the app. Two formats are accepted:
#
#   {"lines": ["{W} ESPRESSO TIME ..."]}   <- human readable text
#
# Each "lines" entry is one board row (max 22 cells, up to 6 rows; missing
# rows stay blank). Letters A-Z, digits and supported punctuation map to
# Vestaboard character codes; anything unrecognized renders blank. Colored
# tiles are markers — {R}ed, {O}range, {Y}ellow, {G}reen, {B}lue, {V}iolet,
# {W}hite, {K} for black, {F} filled — repeated with a count like {Y20}.
#
# Raw code rows are also accepted for copy/paste from the API docs:
#   {"rows": [[...22 codes...], ... 6 rows]}
#
# An optional "title" holds the centered title shown on the live shot screen.
STARTUP_LAYOUT_FILE = os.environ.get("STARTUP_LAYOUT_FILE", "startup_layout.json")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Track previous state for sleep -> wake transition detection
previous_wake_state = None

# Shot lifecycle tracking
shot_active = False
timer_start = None  # set on first preinfusion/pouring substate
frozen_time = None  # final shot time captured at "ending" substate
last_shot_layout = None
_last_shot_end = None

# Ignore shot start/end flapping from the DE1 at shot boundaries so a spurious
# ~0s "phantom" shot (Espresso -> Idle within a few seconds, as the machine
# settles after a real shot) can't overwrite the real final shot time. Real
# espresso shots are always much longer than this and are preceded by a
# multi-second idle/prep gap, so this window is safe.
SHOT_DEBOUNCE_SEC = float(os.environ.get("SHOT_DEBOUNCE_SEC", "15"))

# Serialized send logic:
#  - pending_layout  : the newest layout we want to show
#  - last_sent_layout: the last layout successfully posted to the cloud
#  - _next_allowed   : monotonic time before which we will not post (covers
#                      both the post-interval and any 429 backoff)
# We only publish when pending differs from last_sent AND now >= _next_allowed.
pending_layout = None
last_sent_layout = None
_next_allowed = 0.0
_retry_timer = None

# After a 429 (the board is briefly busy sending a previous message), retry in
# a few seconds rather than waiting a full interval so updates stay fresh.
RETRY_DELAY_SEC = float(os.environ.get("RETRY_DELAY_SEC", "5"))


def schedule_retry(delay=None):
    """Schedule a later attempt so a pending final layout is never dropped."""
    global _retry_timer
    if _retry_timer is not None and _retry_timer.is_alive():
        return
    if delay is None:
        delay = POST_INTERVAL_SEC
    _retry_timer = threading.Timer(delay, maybe_publish)
    _retry_timer.daemon = True
    _retry_timer.start()


def maybe_publish():
    """Publish pending_layout if it is new and the rate-limit allows it."""
    global last_sent_layout, pending_layout, _next_allowed

    if pending_layout is None:
        return

    if pending_layout == last_sent_layout:
        pending_layout = None  # already on the board, nothing to send
        return

    now = time.monotonic()
    if now < _next_allowed:
        schedule_retry(max(_next_allowed - now, 1.0))  # try once window opens
        return

    status = post_layout(pending_layout)
    if status == "ok":
        last_sent_layout = pending_layout
        pending_layout = None
        _next_allowed = time.monotonic() + POST_INTERVAL_SEC
    elif status == "retry":
        _next_allowed = time.monotonic() + RETRY_DELAY_SEC
        schedule_retry(RETRY_DELAY_SEC)  # back off briefly, keep pending
    else:
        # Permanent failure; stop retrying this layout.
        pending_layout = None


def submit_layout(layout):
    """Record the newest layout, then publish it if allowed."""
    global pending_layout
    pending_layout = layout
    maybe_publish()


def get_current_message() -> str | None:
    """Fetch the current message displayed on Vestaboard."""
    if not VESTABOARD_TOKEN:
        logger.error("VESTABOARD_TOKEN not set")
        return None

    try:
        response = requests.get(
            VESTABOARD_CLOUD_API_URL,
            headers={"X-Vestaboard-Token": VESTABOARD_TOKEN},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        # Extract layout from currentMessage
        current_message = data.get("currentMessage", {})
        layout = current_message.get("layout", [])

        if layout:
            # Convert layout to human-readable text
            return layout_to_text(layout)
        return None

    except requests.RequestException as e:
        logger.error(f"Failed to get Vestaboard message: {e}")
        return None


def post_message(message: str) -> bool:
    """Post a message to Vestaboard."""
    if not VESTABOARD_TOKEN:
        logger.error("VESTABOARD_TOKEN not set")
        return False

    try:
        response = requests.post(
            VESTABOARD_CLOUD_API_URL,
            headers={
                "X-Vestaboard-Token": VESTABOARD_TOKEN,
                "Content-Type": "application/json",
            },
            json={"text": message, "forced": True},
            timeout=10,
        )
        response.raise_for_status()
        logger.info(f"Successfully posted message: {message}")
        return True

    except requests.RequestException as e:
        logger.error(f"Failed to post message: {e}")
        return False


def _post_layout_local(layout) -> str:
    """Post a layout via the board's local API."""
    if not VESTABOARD_LOCAL_API_KEY:
        logger.error("VESTABOARD_LOCAL_API_KEY not set")
        return "error"

    response = None
    try:
        response = requests.post(
            f"http://{VESTABOARD_LOCAL_HOST}:{VESTABOARD_LOCAL_PORT}/local-api/message",
            headers={
                "X-Vestaboard-Local-Api-Key": VESTABOARD_LOCAL_API_KEY,
                "Content-Type": "application/json",
            },
            json=layout,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Successfully posted layout (local)")
        return "ok"

    except requests.RequestException as e:
        rate_limited = response is not None and response.status_code == 429
        error_msg = f"Failed to post layout: {e}"
        if response is not None:
            error_msg += f" | Status: {response.status_code} | Response: {response.text}"
        if rate_limited:
            logger.warning(error_msg + " (will retry)")
            return "retry"
        logger.error(error_msg)
        return "error"


def _post_layout_cloud(layout) -> str:
    """Post a layout via the Cloud API (uses Force to override the queue)."""
    if not VESTABOARD_TOKEN:
        logger.error("VESTABOARD_TOKEN not set")
        return "error"

    response = None
    try:
        response = requests.post(
            VESTABOARD_CLOUD_API_URL,
            headers={
                "X-Vestaboard-Token": VESTABOARD_TOKEN,
                "Content-Type": "application/json",
            },
            json={"characters": layout, "forced": True},
            timeout=10,
        )
        response.raise_for_status()
        logger.info("Successfully posted layout (cloud)")
        return "ok"

    except requests.RequestException as e:
        rate_limited = response is not None and response.status_code == 429
        error_msg = f"Failed to post layout: {e}"
        if response is not None:
            error_msg += f" | Status: {response.status_code} | Response: {response.text}"
        if rate_limited:
            logger.warning(error_msg + " (will retry)")
            return "retry"
        logger.error(error_msg)
        return "error"


def post_layout(layout) -> str:
    """Post a layout to Vestaboard.

    Returns "ok", "retry" (rate limited, safe to send again later), or "error".
    Uses the Cloud API or the board's Local API depending on VESTABOARD_API_MODE.
    """
    # Parse layout if it's a string
    if isinstance(layout, str):
        try:
            layout = json.loads(layout)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse layout JSON: {e}")
            return "error"

    if VESTABOARD_API_MODE == "local":
        return _post_layout_local(layout)
    return _post_layout_cloud(layout)


# Official Vestaboard character codes (docs.vestaboard.com/docs/charactercodes).
# Note: the board has NO lowercase letters; only A-Z (1-26), 1-0 (27-36), and
# the punctuation/colors below. Unmapped characters render as blank.
_VB_CODES = {
    " ": 0,
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8, "I": 9,
    "J": 10, "K": 11, "L": 12, "M": 13, "N": 14, "O": 15, "P": 16, "Q": 17,
    "R": 18, "S": 19, "T": 20, "U": 21, "V": 22, "W": 23, "X": 24, "Y": 25,
    "Z": 26,
    "1": 27, "2": 28, "3": 29, "4": 30, "5": 31, "6": 32, "7": 33, "8": 34,
    "9": 35, "0": 36,
    "!": 37, "@": 38, "#": 39, "$": 40, "(": 41, ")": 42, "_": 43, "-": 44,
    "+": 46, "&": 47, "=": 48, ";": 49, ":": 50, "'": 52, '"': 53, "%": 54,
    ",": 55, ".": 56, "/": 59, "?": 60,
}
_VB_INV = {v: k for k, v in _VB_CODES.items()}


# Shot screen column layout (evenly spaced Time / Temp / Vol columns).
# Each header starts with a colored block tile, so its text/value sit one
# column to the right of the block.
HEADER_TIME_COL = 1
HEADER_TEMP_COL = 9
HEADER_VOL_COL = 16

# Official Vestaboard color/tile codes (docs.vestaboard.com/docs/charactercodes).
COLOR_RED = 63
COLOR_ORANGE = 64
COLOR_YELLOW = 65
COLOR_GREEN = 66
COLOR_BLUE = 67
COLOR_VIOLET = 68
COLOR_WHITE = 69
COLOR_BLACK = 70  # local API renders this as white on a white board
COLOR_FILLED = 71  # not available on the local API

# Board-row markers used in startup-layout "lines": {R}{O}{Y}{G}{B}{V}{W}{K}{F}.
_LAYOUT_TILE_MARKERS = {
    "R": COLOR_RED,
    "O": COLOR_ORANGE,
    "Y": COLOR_YELLOW,
    "G": COLOR_GREEN,
    "B": COLOR_BLUE,
    "V": COLOR_VIOLET,
    "W": COLOR_WHITE,
    "K": COLOR_BLACK,
    "F": COLOR_FILLED,
}


def _place(row, text, start):
    """Place left-aligned text into a row starting at column `start`."""
    for i, ch in enumerate(text[: 22 - start]):
        row[start + i] = _VB_CODES.get(ch, 0)


def _text_row(text, center=False):
    """Build a 22-wide row of Vestaboard character codes from text."""
    row = [0] * 22
    if center:
        start = (22 - len(text)) // 2
        _place(row, text, max(start, 0))
    else:
        _place(row, text, 0)
    return row


def _default_startup_layout():
    """Generic startup layout shown when no personalized file is available."""
    return [
        _text_row("WELCOME", center=True),
        _text_row(""),
        _text_row("ESPRESSO", center=True),
        _text_row("READY", center=True),
        [0] * 22,
        [0] * 22,
    ]


DEFAULT_SHOT_TITLE = "ESPRESSO"


def _load_layout_file():
    """Read STARTUP_LAYOUT_FILE; returns None (with a warning) on failure."""
    try:
        with open(STARTUP_LAYOUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("JSON must be an object")
        return data
    except FileNotFoundError:
        logger.warning(f"Startup layout file {STARTUP_LAYOUT_FILE} not found; using defaults")
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to load {STARTUP_LAYOUT_FILE} ({e}); using defaults")
    return None


def _parse_layout_line(line, row_idx):
    """Convert one startup-layout text line into a 22-cell Vestaboard row.

    Characters map through _VB_CODES (unknown chars render blank). Color
    tiles use {R}/{O}/{Y}/{G}/{B}/{V}/{W}/{K}/{F} markers (red, orange,
    yellow, green, blue, violet, white, black, filled) optionally with a
    repeat count like {Y20}. Lines longer than 22 cells are truncated with
    a warning.
    """
    cells = []
    i = 0
    while i < len(line):
        if line[i] == "{":
            end = line.find("}", i)
            if end != -1:
                marker = line[i + 1 : end].upper()
                repeat = 1
                if len(marker) > 1 and marker[1:].isdigit():
                    repeat, marker = int(marker[1:]), marker[0]
                if marker in _LAYOUT_TILE_MARKERS:
                    cells.extend([_LAYOUT_TILE_MARKERS[marker]] * repeat)
                    i = end + 1
                    continue
                logger.warning(
                    f"Unknown tile marker {{{line[i + 1:end]}}} in startup "
                    f"layout row {row_idx}; rendering blank"
                )
                i = end + 1
                continue
        cells.append(_VB_CODES.get(line[i].upper(), 0))
        i += 1
    if len(cells) > 22:
        logger.warning(f"Startup layout row {row_idx} exceeds 22 columns; truncated")
    row = (cells + [0] * 22)[:22]
    return row


def load_startup_layout():
    """Load the wake/startup layout from STARTUP_LAYOUT_FILE.

    Returns a fresh read on every call so file edits take effect without a
    restart. Accepts a human-readable {"lines": [...]} file or raw {"rows":
    [...]} code arrays; falls back to a generic layout if the file is missing
    or invalid.
    """
    data = _load_layout_file()
    if data is None:
        return _default_startup_layout()

    lines = data.get("lines")
    if isinstance(lines, list) and all(isinstance(l, str) for l in lines):
        if len(lines) > 6:
            logger.warning("Startup layout has more than 6 rows; extra rows ignored")
        layout = [_parse_layout_line(l, i) for i, l in enumerate(lines[:6])]
        while len(layout) < 6:
            layout.append([0] * 22)
        return layout

    rows = data.get("rows")
    if (
        isinstance(rows, list)
        and len(rows) == 6
        and all(
            isinstance(row, list)
            and len(row) == 22
            and all(isinstance(c, int) and 0 <= c <= 71 for c in row)
            for row in rows
        )
    ):
        return rows

    logger.warning(f"Invalid startup layout in {STARTUP_LAYOUT_FILE}; using default")
    return _default_startup_layout()


def load_shot_title():
    """Load the shot-screen title from STARTUP_LAYOUT_FILE.

    Reads the file fresh on every call so edits take effect without a
    restart. Failure to read (or the key being absent) is silent — the file
    is also validated separately when the wake screen is displayed.
    """
    data = _load_layout_file()
    if data is not None:
        title = data.get("title")
        if isinstance(title, str) and title.strip():
            return title
    return DEFAULT_SHOT_TITLE


def wrap_text(text, width):
    """Greedy word-wrap `text` into lines of at most `width` characters.

    Words longer than the width are hard-broken across lines. A single-line
    buffer always has room for a trailing line, so callers can rely on getting
    up to `max_lines` entries.
    """
    lines = []
    current = ""
    for word in text.split():
        while len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def build_shot_layout(title, profile, time_s, temp_c, water_ml):
    """Build the 6x22 grid shown on the Vestaboard during a shot.

    `title` is the centered heading (the cafe/shop name from the layout file).
    """
    if time_s is not None:
        t = int(time_s)
        time_str = f"{t // 60}:{t % 60:02d}"
    else:
        time_str = "--"

    temp_str = f"{temp_c:.1f}" if temp_c is not None else "--"
    vol_str = f"{int(round(water_ml))}" if water_ml is not None else "--"

    profile_lines = wrap_text((profile or "").upper(), 22)
    if len(profile_lines) < 2:
        profile_lines.append("")

    layout = [
        _text_row((title or DEFAULT_SHOT_TITLE).upper(), center=True),
        _text_row(""),  # TODAY'S COFFEE: built below
        _text_row(profile_lines[0]),
        _text_row(profile_lines[1]),
        [0] * 22,
        [0] * 22,
    ]

    # Row 1: TODAY'S COFFEE: preceded by an orange block
    layout[1][0] = COLOR_ORANGE
    _place(layout[1], "TODAY'S COFFEE:", 1)

    # Row 4 headers, each preceded by a colored block (TIME=yellow, TEMP=red, VOL=blue)
    layout[4][HEADER_TIME_COL] = COLOR_YELLOW
    _place(layout[4], "TIME", HEADER_TIME_COL + 1)
    layout[4][HEADER_TEMP_COL] = COLOR_RED
    _place(layout[4], "TEMP", HEADER_TEMP_COL + 1)
    layout[4][HEADER_VOL_COL] = COLOR_BLUE
    _place(layout[4], "VOL", HEADER_VOL_COL + 1)

    # Row 5 values, aligned under the labels (one column right of the block)
    _place(layout[5], time_str, HEADER_TIME_COL + 1)
    _place(layout[5], temp_str, HEADER_TEMP_COL + 1)
    _place(layout[5], vol_str, HEADER_VOL_COL + 1)

    return layout


def handle_shot_message(payload):
    """Track the espresso shot lifecycle and refresh the board.

    The start layout is published when we enter the Espresso state (showing
    0:00), but the timer only begins once the DE1 reaches "preinfusion" or
    "pouring" (whichever comes first). It stops on the "ending" substate or
    when we leave the Espresso state.
    """
    global shot_active, timer_start, frozen_time, last_shot_layout, _last_shot_end

    state = payload.get("state", "Unknown")
    substate = payload.get("substate", "") or ""
    profile = payload.get("profile", "")
    temp_c = payload.get("head_temperature")
    water_ml = payload.get("water_level_ml")

    def current_shot_time():
        if frozen_time is not None:
            return frozen_time
        if timer_start is not None:
            return time.monotonic() - timer_start
        return 0

    now = time.monotonic()
    in_espresso = state == "Espresso"
    phase_started = substate in ("preinfusion", "pouring")

    if in_espresso:
        if not shot_active:
            if _last_shot_end is not None and (now - _last_shot_end) < SHOT_DEBOUNCE_SEC:
                logger.info("Ignoring Espresso start inside debounce window (state flapping)")
                return
            shot_active = True
            timer_start = None
            frozen_time = None
            logger.info("Shot started")
        if timer_start is None and phase_started:
            timer_start = now
            logger.info(f"Shot timer started (substate: {substate})")
        if substate == "ending" and frozen_time is None:
            frozen_time = current_shot_time()
            logger.info(f"Shot timer ended (substate: ending): {frozen_time:.0f}s")
        last_shot_layout = build_shot_layout(
            load_shot_title(), profile, current_shot_time(), temp_c, water_ml
        )
        submit_layout(last_shot_layout)
    else:
        if shot_active:
            if timer_start is None:
                # Never reached preinfusion/pouring -> a phantom/flap, not a shot.
                logger.info("Ignoring phantom shot (never reached pouring phase)")
                shot_active = False
                timer_start = None
                frozen_time = None
                _last_shot_end = now
                return
            time_s = current_shot_time()
            shot_active = False
            _last_shot_end = now
            timer_start = None
            frozen_time = None
            last_shot_layout = build_shot_layout(
                load_shot_title(), profile, time_s, temp_c, water_ml
            )
            submit_layout(last_shot_layout)
            logger.info(f"Shot ended; finalized {time_s:.0f}s")




def text_to_vbml_layout(text: str) -> list[list[int]]:
    """Convert text to Vestaboard layout format (6 rows, 22 columns)."""
    # Split into lines
    lines = text.split("\n")

    # Create 6-row layout
    layout = [[0] * 22 for _ in range(6)]

    row = 0
    for line in lines:
        col = 0
        for char in line:
            if col >= 22:
                break
            layout[row][col] = _VB_CODES.get(char.upper(), 0)
            col += 1
        row += 1
        if row >= 6:
            break

    return layout


def layout_to_text(layout) -> str:
    """Convert Vestaboard layout to readable text."""
    if not layout:
        return ""

    lines = []
    for row in layout:
        line = ""
        for char_code in row:
            try:
                code = int(char_code)
            except (ValueError, TypeError):
                code = 0
            line += _VB_INV.get(code, " ")
        lines.append(line.rstrip())
    return "\n".join(lines).rstrip()


def on_connect(client, userdata, flags, rc, properties=None):
    """Callback when connected to MQTT broker."""
    topic = f"{MQTT_PREFIX}/{MQTT_TOPIC}"
    client.subscribe(topic)
    logger.info(f"Connected to MQTT broker and subscribed to {topic}")


def on_message(client, userdata, msg):
    """Callback when MQTT message received."""
    global previous_wake_state

    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except json.JSONDecodeError:
        logger.warning(f"Invalid JSON received: {msg.payload}")
        return

    # Extract wake_state from the message
    wake_state = payload.get("wake_state", None)
    state = payload.get("state", "Unknown")

    logger.info(f"Received state: {state}, wake_state: {wake_state}")

    # Detect sleep -> wake transition
    if wake_state is not None and previous_wake_state is not None:
        if previous_wake_state == False and wake_state == True:
            logger.info("Detected sleep -> wake transition!")

            try:
                submit_layout(load_startup_layout())
                logger.info(f"Queued wake layout from {STARTUP_LAYOUT_FILE}")
            except Exception as e:
                import traceback
                logger.error(f"Error in wake transition handler: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")

    # Update previous state
    previous_wake_state = wake_state

    # Update the live shot screen based on the machine state
    handle_shot_message(payload)


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully."""
    logger.info("Shutting down...")
    sys.exit(0)


def log_startup_layout_status():
    """Log once at startup whether the layout file is locatable and valid.

    The file is still re-read on every wake transition, so a file dropped in
    (or fixed) later is picked up without a restart; this check just makes
    the fallback behavior explicit in the logs rather than waiting for the
    first wake.
    """
    path = os.path.abspath(STARTUP_LAYOUT_FILE)
    if not os.path.isfile(path):
        logger.warning(
            f"Startup layout file not found: {path} -- falling back to the "
            "default wake layout. Provide the file (or fix its path via "
            "STARTUP_LAYOUT_FILE) and it will be picked up without a restart."
        )
        return
    logger.info(f"Startup layout file found: {path} (re-read on every wake)")
    load_startup_layout()  # surfaces any validity warnings before first use


def main():
    """Main entry point."""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Validate configuration
    if VESTABOARD_API_MODE == "local":
        if not VESTABOARD_LOCAL_API_KEY:
            logger.error("VESTABOARD_LOCAL_API_KEY environment variable is required in local mode")
            sys.exit(1)
    elif not VESTABOARD_TOKEN:
        logger.error("VESTABOARD_TOKEN environment variable is required in cloud mode")
        sys.exit(1)

    logger.info("Starting DE1+ to Vestaboard MQTT Bridge")
    log_startup_layout_status()

    # Create MQTT client
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    # Connect and loop
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        client.loop_forever()
    except Exception as e:
        import traceback
        logger.error(f"MQTT loop error: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    main()