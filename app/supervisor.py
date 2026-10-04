"""Keeps one Relay per live source stream: starts, replaces and retires them from what the SRS API reports."""
import json
import threading
import time
import urllib.request

from . import config as C
from .config import log
from .relay import Relay, budget_bytes

relays = {}            # name -> the relay that owns the name (starting/running/draining until noticed)
draining = []          # relays playing out their buffer after their source ended; a successor may run already
relays_lock = threading.Lock()
backoff = {}           # name -> earliest monotonic time a new relay may start (after it died without producing output)
failures = {}          # name -> consecutive relays that never produced output (exponential backoff)
_limit_logged_at = [0.0]
OUT_KBPS = {}          # APP_OUT stream -> kbps SRS received over the last 30 s (0 = publisher connected but silent)
CODEC = {}             # APP_IN stream -> video codec SRS reports ("H264", "HEVC", ...)
HEALTH = {"srs_api_ok": False, "srs_server": None, "srs_major": None,   # srs_server: new id on every SRS restart
          "ffmpeg": ".".join(map(str, C.FFMPEG_VERSION)) if C.FFMPEG_VERSION else None, "pull": C.PULL}
_codec_deferred = set()   # streams seen once without codec info: wait one poll before starting their relay


def codec_decides_pull():
    """True only where the pull path depends on the codec: PULL=auto, SRS < 7 and an ffmpeg that cannot read SRS 6's
    H.265 FLV. Everywhere else every stream is read over HTTP-FLV (or SRT when forced)."""
    return C.PULL == "auto" and not C.FFMPEG_READS_SRS6_HEVC_FLV and (HEALTH["srs_major"] or 0) < 7


def pull_via_srt(codec):
    if C.PULL != "auto":
        return C.PULL == "srt"
    return codec == "HEVC" and codec_decides_pull()


SHUTTING_DOWN = [False]
_api_error_logged_at = [0.0]


def live_streams():
    """Names publishing on APP_IN right now, or None when the SRS API cannot be reached (leave relays alone)."""
    try:
        url = f"{C.SRS_API}/api/v1/streams/?start=0&count=10000"      # the default page is 10 streams
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.load(r)
        streams = [s for s in data.get("streams", []) if s.get("publish", {}).get("active")]
        out_kbps = {s["name"]: (s.get("kbps") or {}).get("recv_30s", 0) for s in streams if s.get("app") == C.APP_OUT}
        codec = {s["name"]: (s.get("video") or {}).get("codec") for s in streams if s.get("app") == C.APP_IN}
        # odd names never reach ffmpeg URLs (they would just flap)
        want = {s["name"] for s in streams if s.get("app") == C.APP_IN and C.name_ok(s["name"])}
    except Exception as e:                       # unreachable API or a malformed payload: unknown, leave relays alone
        HEALTH["srs_api_ok"] = False
        if time.monotonic() - _api_error_logged_at[0] > 30:
            _api_error_logged_at[0] = time.monotonic()
            log("api error:", e)
        return None
    HEALTH["srs_api_ok"] = True
    HEALTH["srs_server"] = data.get("server")
    if HEALTH["srs_major"] is None:
        HEALTH["srs_major"] = _srs_major()
    OUT_KBPS.clear()
    OUT_KBPS.update(out_kbps)
    CODEC.clear()
    CODEC.update(codec)
    return want


def _srs_major():
    """SRS major version (6, 7, ...) from its API, or None when unknown."""
    try:
        with urllib.request.urlopen(f"{C.SRS_API}/api/v1/versions", timeout=3) as r:
            return int(json.load(r)["data"]["major"])
    except Exception:
        return None


def all_relays():
    return list(relays.values()) + draining


def supervise():
    while True:
        try:
            supervise_once()
        except Exception as e:
            log(f"supervise error (continuing): {e}")
        time.sleep(C.POLL_S)


def supervise_once():
    if SHUTTING_DOWN[0]:
        return
    want = live_streams()
    now = time.monotonic()
    with relays_lock:
        _retire_drained(now)
        if want is not None:
            _reconcile(now, want)
        _enforce_budget()


def _retire_drained(now):
    for relay in list(draining):
        if relay.stop:
            draining.remove(relay)
        elif relay.after is not None and not relay.after.stop:
            continue                                      # still waiting for its own predecessor: not stuck
        elif now - relay.drain_started > relay.delay + C.MAX_BUFFER_EXTRA_S + 10:
            log(f"[{relay.name_}] drain stuck (SRS not accepting?), giving up")
            relay.terminate()


def _reconcile(now, want):
    for name in list(relays):
        relay = relays[name]
        if relay.stop:
            if relay.first_tx is None and not relay.ended_by_us:   # died without ever publishing (stale key, ffmpeg missing…)
                failures[name] = failures.get(name, 0) + 1
                backoff[name] = now + min(60, 6 * 2 ** (failures[name] - 1))
            else:
                failures.pop(name, None)
            relay.terminate()
            relays.pop(name, None)
        elif relay.state == "draining":
            relays.pop(name, None)                        # frees the name: a returning source gets a successor at once
            draining.append(relay)
        elif relay.state == "running":
            _check_running(name, relay, now, want)
    for name in [n for n, t in backoff.items() if now >= t]:
        backoff.pop(name, None)
    for name in want:
        if name in relays or now < backoff.get(name, 0):
            continue
        if len(relays) + len(draining) >= C.MAX_STREAMS:
            if now - _limit_logged_at[0] > 60:
                _limit_logged_at[0] = now
                log(f"[{name}] not relayed: MAX_STREAMS={C.MAX_STREAMS} reached")
            break
        predecessor = next((d for d in reversed(draining) if d.name_ == name and not d.stop), None)   # the newest one
        if CODEC.get(name) is None and name not in _codec_deferred:
            # SRS lists a stream before its first key frame / sequence header; a reader started that early gets EOF,
            # dies without output and lands in backoff. Wait one poll for every codec, not only where it picks the path.
            _codec_deferred.add(name)
            continue
        _codec_deferred.discard(name)
        # SRS 6 writes H.265 into FLV as legacy codec id 12. The relay image's ffmpeg 8 reads it, so everything is pulled
        # over HTTP-FLV; with an older ffmpeg, H.265 on SRS < 7 falls back to SRT (adds SRS's SRT latency, ~250 ms).
        via_srt = pull_via_srt(CODEC.get(name))
        relay = Relay(name, C.DELAY_MAP.get(name, C.STATE["default_delay"]), after=predecessor, via_srt=via_srt)
        relays[name] = relay
        relay.start()


def _check_running(name, relay, now, want):
    """Liveness of one running relay; every problem ends in end_source() (play out, then the supervisor restarts)."""
    if name not in want:
        relay.end_source()                                # source gone: play out the buffered tail, then exit
        return
    silent_for = now - relay.last_rx
    if silent_for > (15 if relay.rx_bytes else 30):      # listed but silent: stalled socket
        log(f"[{name}] no data from the source for {silent_for:.0f} s while SRS lists it, restarting")
        relay.end_source()
        return
    # Dead publish: we have been writing for > 60 s (SRS's 30 s rate window is full), input flows, yet SRS
    # hears nothing on APP_OUT. Three consecutive polls so a single stale reading cannot trigger it.
    silent_publish = (relay.first_tx is not None and now - relay.first_tx > 60
                      and now - relay.last_rx < 5 and OUT_KBPS.get(name, 0) == 0)
    relay.zero_kbps = relay.zero_kbps + 1 if silent_publish else 0
    if relay.zero_kbps >= 3:
        log(f"[{name}] SRS receives nothing on {C.APP_OUT}/{name} for 3 polls although the source flows, restarting")
        relay.end_source()


def _enforce_budget():
    over = budget_bytes() - C.MAX_BUFFER_BYTES
    if over <= 0:
        return
    fattest = max(all_relays(), key=lambda r: r.q_bytes, default=None)
    if fattest and fattest.q_bytes:
        fattest.drop_oldest(over)
