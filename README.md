# DE1Vestaboard

Dockerized DE1+ to Vestaboard MQTT bridge.

Subscribes to the DE1+ MQTT state topic and displays status on a Vestaboard:

- On a **sleep → wake transition**, posts the layout from `startup_layout.json` (see the [Cookbook](#cookbook--examples) for the format — it's plain text, no character codes to count).
- During an **espresso shot** (`state == "Espresso"`), posts a live shot screen:
  - The shop title from `startup_layout.json` (`"title"`, centered — defaults to `ESPRESSO`)
  - `TODAY'S COFFEE:` + the current shot `profile` name
  - `TIME  TEMP  VOL` headers with live values
    - **Time** shows `0:00` the moment the shot enters the `Espresso` state, then starts counting only once the DE1 reaches the `preinfusion` or `pouring` substate. It stops on the `ending` substate or when the machine leaves `Espresso`.
    - **Temp** is the DE1 `head_temperature`.
    - **Vol** is the tank water level (`water_level_ml`) — the shot-scale weight is **not** exposed by the `de1plus-mqtt` plugin, so this is a tank indicator, not shot yield.

---

## Getting started

This is a self-serve guide for running the bridge with your own equipment. Nothing here depends on a particular machine, network, or registry.

### Prerequisites

- A **DE1+** espresso machine running the [de1plus-mqtt plugin](https://github.com/dscho/de1plus-mqtt), publishing state to an **MQTT broker** that this bridge can reach.
- A **Vestaboard** with either:
  - the **Cloud API** — a token from the Developer section of the [Vestaboard web app](https://docs.vestaboard.com), or
  - the **Local API** — the board's on-LAN HTTP API (see [Local API mode](#local-api-mode)).
- **Docker** (or Python 3.11+ if you want to run it directly).

### Step 1: Get a Vestaboard credential

**Cloud mode (default).** Generate a read/write token from the Developer section of the Vestaboard web app. It is passed to the bridge as `VESTABOARD_TOKEN`.

**Local mode (recommended — no cloud round-trip, faster updates).** Enable the Local API once; the board owner receives an enablement token by email, then:

```bash
curl -X POST \
  -H "X-Vestaboard-Local-Api-Enablement-Token: YOUR_ENABLEMENT_TOKEN" \
  http://your-board.local:7000/local-api/enablement
# -> {"message":"Local API enabled","apiKey":"..."}
```

The returned `apiKey` is passed as `VESTABOARD_LOCAL_API_KEY`.

> mDNS `.local` names often don't resolve inside a container. If so, use the board's IP address for `VESTABOARD_LOCAL_HOST`, or run the container with `--network host`.

### Step 2: Build

```bash
docker build -t de1vestaboard:latest .
```

On a build host with a different architecture than the target's (e.g. building for a Raspberry Pi or SBC from an x86 machine), use Docker Buildx per the [multi-platform docs](https://docs.docker.com/build/concepts/multi-platform/):

```bash
docker buildx build --platform linux/arm64 -t de1vestaboard:latest .
```

### Step 3: Run

Cloud mode:

```bash
docker run -d --name de1vestaboard \
  -e MQTT_BROKER=<your-broker-host> \
  -e VESTABOARD_TOKEN=<your-token> \
  de1vestaboard:latest
```

Local mode:

```bash
docker run -d --name de1vestaboard \
  -e MQTT_BROKER=<your-broker-host> \
  -e VESTABOARD_API_MODE=local \
  -e VESTABOARD_LOCAL_HOST=<board-ip-or-hostname> \
  -e VESTABOARD_LOCAL_API_KEY=<the-api-key> \
  de1vestaboard:latest
```

Or with Docker Compose:

```yaml
services:
  de1vestaboard:
    build: .
    container_name: de1vestaboard
    restart: unless-stopped
    environment:
      - MQTT_BROKER=<your-broker-host>
      - VESTABOARD_API_MODE=local
      - VESTABOARD_LOCAL_HOST=<board-ip-or-hostname>
      - VESTABOARD_LOCAL_API_KEY=<the-api-key>
    volumes:
      # Makes the startup screen editable without rebuilding the image
      - ./startup_layout.json:/app/startup_layout.json:ro
```

### Running without Docker

```bash
python -m venv .venv
. .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MQTT_BROKER=<your-broker-host>
export VESTABOARD_TOKEN=<your-token>
python app.py
```

The working directory must contain `startup_layout.json` (or point `STARTUP_LAYOUT_FILE` at it) — see the [config table](#configuration-environment-variables).

### Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_BROKER` | *(none — set it)* | MQTT broker address |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_PREFIX` | `de1` | MQTT topic prefix |
| `MQTT_TOPIC` | `state` | MQTT state topic (full topic is `<prefix>/<topic>`) |
| `VESTABOARD_API_MODE` | `cloud` | `cloud` ([cloud.vestaboard.com](https://cloud.vestaboard.com)) or `local` (board's on-LAN local API) |
| `VESTABOARD_TOKEN` | — | **Cloud mode:** Vestaboard cloud API token |
| `VESTABOARD_LOCAL_HOST` | *(none — set it)* | **Local mode:** board hostname/IP |
| `VESTABOARD_LOCAL_PORT` | `7000` | **Local mode:** board local API port |
| `VESTABOARD_LOCAL_API_KEY` | — | **Local mode:** the Local API key (see above) |
| `STARTUP_LAYOUT_FILE` | `startup_layout.json` | Path to the wake-screen layout file; located once at startup (with a log line stating found or fallback) and re-read on every wake transition, so edits take effect without restarting |
| `POST_INTERVAL_SEC` | `16` | Minimum seconds between Vestaboard posts. The board physically updates about once every 15s, so 16 keeps a little headroom. A layout is only sent when the pending one differs from the last successfully posted one; rate-limited (429) posts are retried so the final shot time is never dropped |
| `RETRY_DELAY_SEC` | `5` | When the board returns a 429 (busy sending a previous message), wait this long before retrying the pending layout. Keeps live updates fresh without hammering the API |
| `SHOT_DEBOUNCE_SEC` | `15` | Ignore DE1 start/end flapping near shot boundaries so a spurious ~0s "phantom" shot can't overwrite the real final shot time |

### Notes & limitations

- The DE1 community `de1plus-mqtt` plugin publishes **immediately on every state/substate change** (e.g. wake, and each espresso phase: `preinfusion` → `pouring` → `ending`), and additionally sends a heartbeat every `publish_interval_ms` (default 60000) when nothing changes. Mid-shot updates are event-driven and no plugin setting needs to change; the bridge rate-limits Vestaboard posts itself via `POST_INTERVAL_SEC`.
- The plugin does **not** publish per-shot scale weight or a shot clock, so shot duration is measured locally (displayed as `m:ss`) and "Vol" maps to tank water level. To show true shot weight, you'd need a source that forwards `espresso_weight` (e.g. Decent's local `web_api` plugin or a custom MQTT bridge).
- The Vestaboard has **no lowercase letters** — all display text is uppercased, and only characters in the official character set render.

---

## Contributors guide

### Architecture

```
 DE1+  ──(de1plus-mqtt plugin)──▶  MQTT broker  ──▶  this bridge  ──HTTPS──▶  Vestaboard
                                   de1/state                      (cloud or local API)
                                                  ┌────────────────────────┐
                                                  │ on_message             │
                                                  │  ├ wake transition ────┼──▶ startup layout (from JSON file)
                                                  │  ├ shot lifecycle ─────┼──▶ live shot screen
                                                  │  └ publish queue ──────┼──▶ rate-limited sends + 429 retry
                                                  └────────────────────────┘
```

Everything lives in `app.py`. Reading top to bottom:

| Section | What it does |
|---|---|
| Config block | All env-driven settings; no code changes needed to redeploy elsewhere |
| Publish queue (`submit_layout` / `maybe_publish` / `schedule_retry`) | Layer between callers and the API. The newest layout becomes `pending_layout` (newer layouts supersede older ones); a background timer retries until `pending` differs from `last_sent` **and** the rate window has opened. 429s back off for `RETRY_DELAY_SEC`; permanent errors drop the pending layout |
| Vestaboard transports (`post_layout` → `_post_layout_cloud` / `_post_layout_local`) | Post the same 6×22 code grid over either API. Both treat 429 as retryable and everything else as fatal |
| Character mapping (`_VB_CODES`, `_VB_INV`) | Bidirectional map between display text and official character codes. Unknown characters render blank — there is no lowercase on the board |
| Shot layout (`build_shot_layout`, `wrap_text`) | Builds the live shot screen: centered title (from the layout file's `"title"`), orange `TODAY'S COFFEE:` row, two wrapped profile-name rows, colored `TIME/TEMP/VOL` headers (yellow/red/blue) and the value row beneath them |
| Startup layout (`load_startup_layout`, `load_shot_title`) | Reads and validates `startup_layout.json` fresh on every use — both the wake screen and the shot-screen title — so the personalized content is editable without a restart |
| Shot lifecycle (`handle_shot_message`) | State machine over MQTT payloads, detailed below |
| MQTT (`on_connect`, `on_message`, `main`) | Subscribe to `{MQTT_PREFIX}/{MQTT_TOPIC}`, dispatch, validate credentials at startup, run the network loop |

### Wake screen

On a `wake_state: false → true` edge, `on_message` loads `load_startup_layout()` and submits it through the publish queue. Because the file is re-read on each transition, edits to the JSON appear on the next wake, no restart. If the file is missing, malformed, or out of spec, a generic built-in layout is used and the reason is logged.

At startup (`log_startup_layout_status()`), the bridge logs the resolved absolute path of `STARTUP_LAYOUT_FILE` — either confirming it found a valid file, or explicitly stating it is falling back to the default wake layout until the file is provided (or the `STARTUP_LAYOUT_FILE` path is corrected), no restart needed either way.

### Shot lifecycle and the anti-phantom debounce

The DE1 plugin can flap state at shot boundaries (a brief `Espresso → Idle` right after a real shot). To keep a bogus `0:00` screen from overwriting a real one:

- Entering `Espresso` starts a shot, shows `0:00`, but the **timer only starts** at the first `preinfusion` or `pouring` substate.
- The timer **freezes** at the `ending` substate.
- Leaving `Espresso` finalizes the screen with the frozen time.
- A shot that never reached `preinfusion`/`pouring` is discarded as a phantom.
- A restart within `SHOT_DEBOUNCE_SEC` of the previous end is ignored as flapping.

Real espresso shots are much longer than the debounce window and are preceded by a multi-second idle/prep gap, so the window is safe.

### Rate limiting

Vestaboard's board physically refreshes roughly every 15s. The bridge enforces at least `POST_INTERVAL_SEC` between successful posts and never sends a layout identical to the last posted one. `pending_layout` always holds the newest content, so a rapid burst of updates collapses into the most recent one when the window opens.

### Startup layout file

Two formats are accepted (and the loader validates both, falling back to a default on any problem):

- `"lines"` — human-readable text, one entry per board row, using `{R}{O}{Y}{G}{B}{V}{W}{K}{F}` markers for colored tiles (repeatable, e.g. `{Y20}`). See the Cookbook.
- `"rows"` — raw 6×22 arrays of official character codes, convenient for copy/paste from the character-code docs.

An optional `"title"` string sets the centered heading of the live shot screen (defaults to `ESPRESSO` when absent). The file is read fresh for both the wake screen and the title, so all of it is hot-editable.

### Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# point at any broker; a local mosquitto is fine
export MQTT_BROKER=localhost
export VESTABOARD_API_MODE=local
export VESTABOARD_LOCAL_HOST=<board-ip>
export VESTABOARD_LOCAL_API_KEY=<key>
python app.py
```

Publish synthetic messages to exercise behavior without touching the machine:

```bash
mosquitto_pub -h localhost -t de1/state -m '{"state":"Espresso","substate":"pouring","profile":"Test Shot","head_temperature":93.5,"water_level_ml":720}'
```

Logs go to stdout (`INFO` level) and include every state change, timer start/stop, post result, and each fallback reason.

Container builds are pinned to `python:3.11-slim` and run as a non-root `appuser`; the only runtime dependencies are `paho-mqtt` and `requests`.

### Conventions

- Match the existing style: module-level config constants, small pure helpers, docstrings on anything non-obvious.
- All Vestaboard geometry (6 rows × 22 columns) and the character/color constants come from the official docs — do not invent codes.
- Keep behavior-deciding tunables as env variables with sane defaults.

---

## Cookbook & examples

### The startup screen

`startup_layout.json` is plain text. Each entry is a board row (max 6 rows, 22 cells each); shorter rows are padded with blanks. Colors are markers that count as one cell:

| Marker | Tile | Emoji stand-in |
|--------|------|----------------|
| `{R}` | red | 🟥 |
| `{O}` | orange | 🟧 |
| `{Y}` | yellow | 🟨 |
| `{G}` | green | 🟩 |
| `{B}` | blue | 🟦 |
| `{V}` | violet | 🟪 |
| `{W}` | white | ⬜ |
| `{K}` | black | ⬛ |
| `{F}` | filled (blank tile) | ⬜ |

Markers accept a repeat count (`{Y20}` = 20 yellows). Text renders A–Z, digits, and supported punctuation; anything else (including lowercase) is blanked. A single space between words is a blank cell.

To seed your own wake screen, adapt an example like the one below into your `startup_layout.json` (the same file's optional `"title"` also becomes the live shot-screen heading):

```json
{
  "title": "CORNER CAFE",
  "lines": [
    "{G}{G}CORNER CAFE MENU{G}{G}",
    "{G}{Y20}{G}",
    "ESPRESSO   LATTE",
    "AMERICANO  MOCHA",
    "POUROVER   MATCHA",
    ""
  ]
}
```

renders as:

```
🟩🟩CORNER CAFE MENU🟩🟩
🟩🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟨🟩
ESPRESSO   LATTE
AMERICANO  MOCHA
POUROVER   MATCHA
```

A simpler example using colored accents on a two-column menu:

```json
{
  "lines": [
    "{Y}TODAY:",
    "AMERICANO  ",
    "{Y}SPECIALS:",
    "BAKLAVA    ",
    "",
    "{G}ENJOY!"
  ]
}
```

```
🟨TODAY:
AMERICANO
🟨SPECIALS:
BAKLAVA

🟩ENJOY!
```

Editing tips:

- Keep `{...}` markers around the *outside* of text; the character inside the braces is not displayed.
- Rows longer than 22 cells are truncated (a warning is logged) — the emoji grid above is 22 columns wide.
- The raw-code format `{"rows": [[...], ...]}` is also accepted, handy for copying a layout straight out of the character-code documentation.

### The live shot screen

Built automatically during a shot. With `profile = "Melange"` mid-shot it reads:

```
     CORNER CAFE        ← centered title from "title" (defaults to ESPRESSO)
🟧TODAY'S COFFEE:
MELANGE                 ← profile name (wraps to 2 rows if long)

⬛🟨TIME⬛⬛⬛🟥TEMP⬛⬛🟦VOL⬛⬛
⬛⬛1:32⬛⬛⬛⬛93.5⬛⬛⬛720⬛⬛
```

(Long profile names wrap onto two lines; the value row updates live until the timer freezes at the `ending` substate.)

### MQTT payload reference

The bridge subscribes to `<MQTT_PREFIX>/<MQTT_TOPIC>` (default `de1/state`). A typical shot message:

```json
{
  "state": "Espresso",
  "substate": "pouring",
  "wake_state": true,
  "profile": "Melange",
  "head_temperature": 93.5,
  "water_level_ml": 720
}
```

Fields used: `state`, `substate`, `wake_state`, `profile`, `head_temperature`, `water_level_ml`. Anything else in the payload is ignored.

---

## Resources & references

Documentation worth reading before extending or configuring:

- **Vestaboard developer docs** (Cloud API, Local API, VBML): <https://docs.vestaboard.com>
- **Character codes** — the definitive list this project maps text and colors against: <https://docs.vestaboard.com/docs/charactercodes>
- **Local API** — enablement and endpoints: <https://docs.vestaboard.com/docs/local-api/authentication> and <https://docs.vestaboard.com/docs/local-api/endpoints>
- **Cloud API** — `POST https://cloud.vestaboard.com/` with `X-Vestaboard-Token`: see the developer docs above
- **DE1+ MQTT plugin** — the state source and its payload format: <https://github.com/dscho/de1plus-mqtt>
- **paho-mqtt** (the MQTT client used): <https://eclipse.dev/paho/index.php?page=clients/python/index.php>
- Building multi-arch images with **Docker Buildx**: <https://docs.docker.com/build/concepts/multi-platform/>
