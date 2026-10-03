"""Entry point: load state, start the supervisor, serve the control API, play out buffers on SIGTERM."""
import os
import signal
import threading
import time
from http.server import ThreadingHTTPServer

from . import config as C
from . import supervisor as S
from .api import Control
from .config import log


def main():
    C.validate_env()
    C.state_load()
    if not C.token_ok(C.TOKEN["publish"]):
        log(f"WARNING: PUBLISH_TOKEN is unset/invalid (must match {C.TOKEN_RE}, not change-me*): "
            f"every publish through the edge is DENIED until PUT /edge/token")
        C.TOKEN["publish"] = ""
    token_info = "MISSING"
    if C.TOKEN["publish"]:
        token_info = f"from {C.TOKEN['source']}"
        if C.TOKEN["source"] != "env":
            token_info += " (env value ignored until DELETE /state)"
    log(f"delay_relay up: {C.APP_IN} -> {C.APP_OUT}, default {C.STATE['default_delay']}s, map {C.DELAY_MAP}, "
        f"control :{C.CONTROL_PORT}, publish token {token_info}, own address {C.OWN_IP or 'unknown (hook ip check off)'}")
    threading.Thread(target=S.supervise, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", C.CONTROL_PORT), Control).serve_forever()


def shutdown(*_):
    """SIGTERM: stop reading, play out what is buffered (viewers keep the tail), then exit. Bounded by the
    longest delay + 15 s and by STOP_GRACE_SECONDS (compose sends SIGKILL after stop_grace_period).
    Runs in its own thread so the control API (and the edge's /auth checks) keep working meanwhile."""
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))
    S.SHUTTING_DOWN[0] = True                           # the supervisor must not start successors now
    threading.Thread(target=_drain_and_exit, daemon=True).start()


def _drain_and_exit():
    with S.relays_lock:
        active = S.all_relays()
    for relay in active:
        relay.end_source()
    longest = max([r.delay for r in active] + [0.0])
    deadline = time.monotonic() + min(longest + 15, max(C.STOP_GRACE_S - 2, 1))
    if active:
        log(f"shutdown: playing out {len(active)} relay buffer(s) first (up to {deadline - time.monotonic():.0f}s)")
    while any(not r.stop for r in active) and time.monotonic() < deadline:
        time.sleep(0.2)
    for relay in active:
        relay.terminate()
    os._exit(0)


def run():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    main()
