"""Environment, validation, persisted runtime state (state.json) and the SRT latency file."""
import json
import os
import re
import secrets
import socket
import sys
import threading
import time


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


# --- SRS endpoints and app names ------------------------------------------------------------------
SRS_API = os.environ.get("SRS_API", "http://srs:1985")
SRS_HTTP = os.environ.get("SRS_HTTP", "http://srs:8080")
SRS_RTMP = os.environ.get("SRS_RTMP", "rtmp://srs:1935")
SRS_SRT = os.environ.get("SRS_SRT", "srt://srs:10080")   # HEVC sources are pulled over SRT (see relay._spawn)
APP_IN = os.environ.get("APP_IN", "source")      # encoders publish here; the control room pulls here (no delay)
APP_OUT = os.environ.get("APP_OUT", "live")      # the delayed copy; the edge lets outsiders play it

NAME_RE = r"[A-Za-z0-9_.-]{1,64}"               # app / stream names that reach ffmpeg URLs and nginx
TOKEN_RE = r"[A-Za-z0-9_.-]{8,128}"             # URL-safe without encoding
# Our own publishes to APP_OUT carry ?relay=<this>; SRS's on_publish hook lets nothing else into APP_OUT
# (an SRT encoder could otherwise publish straight into the public app, skipping token and delay).
RELAY_SECRET = secrets.token_urlsafe(18)


def own_address():
    """This container's address on the compose network (SRS reports the publisher's ip in its hook), or None."""
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return None


OWN_IP = os.environ.get("OWN_IP", own_address()) or None   # OWN_IP="" disables the source-address check


def name_ok(name):
    return bool(re.fullmatch(NAME_RE, name or ""))


def token_ok(token):
    """A usable publish token: matches TOKEN_RE and is not the compose placeholder."""
    return bool(re.fullmatch(TOKEN_RE, token or "")) and not token.lower().startswith("change-me")


# --- limits ---------------------------------------------------------------------------------------
MAX_DELAY_S = float(os.environ.get("MAX_DELAY_SECONDS", "300"))        # RAM per stream ~= bitrate * delay
MAX_BUFFER_BYTES = int(float(os.environ.get("MAX_BUFFER_MB", "1024")) * 1048576)   # all relays together
MAX_BUFFER_EXTRA_S = float(os.environ.get("MAX_BUFFER_EXTRA_SECONDS", "15"))   # cap when the consumer stalls
MAX_STREAMS = int(os.environ.get("MAX_STREAMS", "32"))
# Longest output pause a live delay increase may cause. Must stay below SRS's publish normal_timeout
# (5000 ms in docker-compose.yml) or SRS drops the idle publisher.
MAX_PAUSE_S = float(os.environ.get("MAX_PAUSE_SECONDS", "3"))
POLL_S = float(os.environ.get("POLL_SECONDS", "3"))
STOP_GRACE_S = float(os.environ.get("STOP_GRACE_SECONDS", "60"))        # = compose stop_grace_period

CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "9090"))
CONTROL_TOKEN = os.environ.get("CONTROL_TOKEN", "")
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
SRT_ENV_FILE = os.environ.get("SRT_ENV_FILE", "/config/srt.env")
SRT_KEYS = ("SRS_SRT_SERVER_LATENCY", "SRS_SRT_SERVER_RECVLATENCY", "SRS_SRT_SERVER_PEERLATENCY")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
CHUNK = 188 * 7                                  # one read from the source ffmpeg = 7 TS packets


def parse_delay(value):
    """A delay must be finite and 0..MAX_DELAY_S: 'inf' or a huge value would make the RAM buffer unbounded."""
    seconds = float(value)
    if not 0.0 <= seconds <= MAX_DELAY_S:
        raise ValueError(f"delay must be 0..{MAX_DELAY_S:.0f} s")
    return seconds


def _parse_delay_map(text):
    out = {}
    for item in text.split(","):
        if "=" in item:
            name, value = item.split("=", 1)
            name = name.strip()
            if not name_ok(name):
                raise ValueError(f"DELAY_MAP: stream name {name!r} must match {NAME_RE}")
            out[name] = parse_delay(value.strip())
    return out


def _env_or_exit(label, fn):
    try:
        return fn()
    except ValueError as e:
        sys.exit(f"{label}: {e}")


def validate_env():
    for key, value in (("APP_IN", APP_IN), ("APP_OUT", APP_OUT)):
        if not name_ok(value):
            sys.exit(f"{key}={value!r} must match {NAME_RE}")
    if APP_IN == APP_OUT:
        sys.exit("APP_IN and APP_OUT must differ")


# --- runtime state (env defaults, overridden by state.json) ---------------------------------------
STATE = {
    "default_delay": _env_or_exit("DELAY_SECONDS", lambda: parse_delay(os.environ.get("DELAY_SECONDS", "2"))),
    "srt_pending_server": None,                  # SRS server id when srt.env was last written (None = nothing pending)
}
ENV_DELAY_MAP = _env_or_exit("DELAY_MAP", lambda: _parse_delay_map(os.environ.get("DELAY_MAP", "")))
DELAY_MAP = dict(ENV_DELAY_MAP)                  # effective per-stream delays
DELAY_OVERRIDES = {}                             # the ones set through the API (persisted, win over env)
ENV_TOKEN = os.environ.get("PUBLISH_TOKEN", "")
TOKEN = {"publish": ENV_TOKEN, "source": "env"}

_state_lock = threading.Lock()


def state_load():
    """Overrides made through the API survive restarts and win over the env defaults."""
    try:
        with open(STATE_FILE) as f:
            saved = json.load(f)
    except Exception:
        return
    try:
        if saved.get("publish_token"):
            if token_ok(saved["publish_token"]):
                TOKEN["publish"] = saved["publish_token"]
                TOKEN["source"] = "state.json"
            else:
                log("state.json holds an invalid publish token; keeping the env value")
        if saved.get("default_delay") is not None:
            STATE["default_delay"] = parse_delay(saved["default_delay"])
        if saved.get("srt_pending_server"):
            STATE["srt_pending_server"] = saved["srt_pending_server"]
        DELAY_OVERRIDES.update({k: parse_delay(v) for k, v in (saved.get("delay_map") or {}).items()})
        DELAY_MAP.update(DELAY_OVERRIDES)
        log(f"state loaded from {STATE_FILE}: {sorted(saved)}")
    except Exception as e:
        log(f"state file {STATE_FILE} is invalid ({e}); using env defaults for the rest")


def state_save(**changes):
    """Read-modify-write under a lock (API handlers run in parallel threads), atomic replace."""
    with _state_lock:
        try:
            try:
                with open(STATE_FILE) as f:
                    saved = json.load(f)
            except Exception:
                saved = {}
            saved.update(changes)
            os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(saved, f)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log(f"state save failed ({e}) — change applied but will not survive a restart")


def reset_overrides():
    """DELETE /state: per-stream delays and the publish token go back to the env values now."""
    DELAY_OVERRIDES.clear()
    DELAY_MAP.clear()
    DELAY_MAP.update(ENV_DELAY_MAP)
    TOKEN["publish"] = ENV_TOKEN if token_ok(ENV_TOKEN) else ""
    TOKEN["source"] = "env"


# --- SRT latency file (compose feeds it to srs via env_file) --------------------------------------
def srt_env_get():
    """Latency (ms) in SRT_ENV_FILE, or None when the file is missing or unreadable."""
    try:
        with open(SRT_ENV_FILE) as f:
            match = re.search(rf"(?m)^{SRT_KEYS[0]}=(\d+)", f.read())
    except OSError:
        return None
    return int(match.group(1)) if match else None


def srt_env_set(ms):
    """Write the three SRS_SRT_SERVER_*LATENCY keys (all three, or SRS ignores the value) with an atomic
    replace inside the bind-mounted directory. SRS picks it up on its next `docker compose up -d srs`."""
    with _state_lock:
        tmp = SRT_ENV_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write("".join(f"{key}={ms}\n" for key in SRT_KEYS))
        os.replace(tmp, SRT_ENV_FILE)
