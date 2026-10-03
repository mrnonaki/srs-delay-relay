"""Control API (JSON over HTTP). /health and the SRS hooks (/srs/on_publish for the origin, /srs/edge_on_publish and
/srs/edge_on_play for the edge) are open; everything else needs CONTROL_TOKEN (compose makes it mandatory)."""
import hmac
import json
import os
import re
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

from . import config as C
from . import supervisor as S
from .config import log

_deny_logged_at = [0.0]


class Control(BaseHTTPRequestHandler):
    timeout = 10                       # an idle connection must not pin a handler thread
    server_version = "delay-relay"
    sys_version = ""

    # --- helpers ------------------------------------------------------------------------------
    def log_message(self, *args):
        pass

    def handle_one_request(self):
        """No handler may ever drop the connection with a traceback: unexpected errors become a 500."""
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:
            log(f"control API error on {self.path[:80]}: {e}")
            try:
                self._send(500, {"error": "internal error"})
            except Exception:
                pass

    def _path(self):
        return urllib.parse.urlparse(self.path).path

    def _query(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

    def _authed(self):
        if not C.CONTROL_TOKEN:
            return True
        supplied = self.headers.get("Authorization", "")[len("Bearer "):] or self._query().get("auth", "")
        return hmac.compare_digest(supplied.encode(), C.CONTROL_TOKEN.encode())

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- GET ----------------------------------------------------------------------------------
    def do_GET(self):
        path = self._path()
        if path == "/health":
            return self._send(200, {"ok": True, "relays": len(S.relays), **S.HEALTH})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if path == "/streams":
            return self._streams()
        if path == "/delay":
            return self._send(200, {"default_delay": C.STATE["default_delay"], "map": C.DELAY_MAP})
        if path == "/srs/srt-latency":
            return self._srt_latency()
        if path == "/edge":
            return self._send(200, {"token_len": len(C.TOKEN["publish"]), "token_source": C.TOKEN["source"],
                                    "app_in": C.APP_IN, "app_out": C.APP_OUT})      # never the token itself
        self._send(404, {"error": "not found"})

    def _streams(self):
        with S.relays_lock:                    # build under the lock, send outside it: a slow client must not stall the supervisor
            out = [{"name": r.name_, "state": r.state, "delay": r.delay, "buffered_s": r.buffered_s(),
                    "rx_bytes": r.rx_bytes, "tx_bytes": r.tx_bytes, "uptime_s": round(time.monotonic() - r.started)}
                   for r in S.all_relays()]
        self._send(200, {"streams": out})

    def _srt_latency(self):
        value = C.srt_env_get()
        pending_id = C.STATE.get("srt_pending_server")
        if not pending_id:
            status = "no change pending"
        elif pending_id == "unknown":
            status = "unknown: SRS API was unreachable when the file was written; run scripts/apply-srt.sh"
        elif pending_id == S.HEALTH["srs_server"]:
            status = "pending: run scripts/apply-srt.sh or `docker compose up -d srs`"
        else:
            status = "SRS restarted since the change (apply-srt.sh / compose up -d applies it; a crash restart does not)"
        self._send(200 if value is not None else 404, {"srt_latency_ms": value, "file": C.SRT_ENV_FILE, "status": status})

    # --- PUT ----------------------------------------------------------------------------------
    def do_PUT(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        path = self._path()
        q = self._query()
        if path == "/edge/token":
            return self._set_token(q.get("value", ""))
        if path == "/srs/srt-latency":
            return self._set_srt_latency(q.get("ms"))
        try:
            seconds = C.parse_delay(q.get("seconds"))
        except (TypeError, ValueError) as e:
            return self._send(400, {"error": f"seconds=N required ({e})"})
        if path == "/delay":
            return self._set_default_delay(seconds, q.get("apply") == "all")
        if path.startswith("/delay/"):
            return self._set_stream_delay(path[len("/delay/"):], seconds)
        self._send(404, {"error": "not found"})

    def _set_token(self, token):
        if not C.token_ok(token):
            return self._send(400, {"error": f"value must match {C.TOKEN_RE} and not be a change-me placeholder"})
        C.TOKEN["publish"] = token
        C.TOKEN["source"] = "state.json"
        C.state_save(publish_token=token)
        log("publish token rotated (effective for new publishes; env value ignored until DELETE /state)")
        self._send(200, {"token_len": len(token), "note": "publishers already connected stay connected until they reconnect"})

    def _set_srt_latency(self, ms):
        try:
            ms = int(ms)
        except (TypeError, ValueError):
            return self._send(400, {"error": "ms=N required"})
        if not 1 <= ms <= 60000:
            return self._send(400, {"error": "ms must be 1..60000"})
        try:
            C.srt_env_set(ms)
        except Exception as e:
            return self._send(500, {"error": f"cannot write {C.SRT_ENV_FILE}: {e}"})
        marker = S.HEALTH["srs_server"] or "unknown"
        C.STATE["srt_pending_server"] = marker
        C.state_save(srt_pending_server=marker)
        self._send(200, {"srt_latency_ms": ms, "status": "pending",
                         "apply": "scripts/apply-srt.sh (cron) or `docker compose up -d srs`",
                         "note": "SRS restarts (~3 s); publishers reconnect"})

    def _set_default_delay(self, seconds, apply_all):
        C.STATE["default_delay"] = seconds
        C.state_save(default_delay=seconds)
        applied = {}
        if apply_all:
            with S.relays_lock:
                for name, relay in S.relays.items():
                    C.DELAY_MAP[name] = C.DELAY_OVERRIDES[name] = seconds   # survives the restart path too
                    applied[name] = relay.set_delay(seconds)                # live / restart / ignored
            C.state_save(delay_map=dict(C.DELAY_OVERRIDES))
        self._send(200, {"default_delay": seconds, "applied_to_running": applied if apply_all else None})

    def _set_stream_delay(self, name, seconds):
        if not C.name_ok(name):
            return self._send(400, {"error": "stream name required"})
        C.DELAY_MAP[name] = C.DELAY_OVERRIDES[name] = seconds
        C.state_save(delay_map=dict(C.DELAY_OVERRIDES))
        with S.relays_lock:
            relay = S.relays.get(name)
        how = relay.set_delay(seconds) if relay else "ignored"
        notes = {"live": None, "restart": "buffer plays out, then the relay restarts with the new delay",
                 "ignored": "takes effect when the stream (re)starts"}
        self._send(200, {"name": name, "delay": seconds, "running": bool(relay), "applied_now": how == "live", "note": notes[how]})

    # --- POST (SRS http_hooks; SRS allows on HTTP 200 + {"code": 0}, denies on anything else) ---------------
    def do_POST(self):
        path = self._path()
        if path not in ("/srs/on_publish", "/srs/edge_on_publish", "/srs/edge_on_play"):
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)) or b"{}")
        except Exception:
            body = {}
        app, stream, param = body.get("app", ""), body.get("stream", ""), body.get("param", "") or ""
        if path == "/srs/on_publish":                            # origin: who may publish on the LAN-side SRS
            if app == C.APP_IN:
                ok = C.name_ok(stream)                           # encoders (SRT, or RTMP forwarded by the edge)
            elif app == C.APP_OUT:                               # only our own relays may publish the public app:
                ok = (f"relay={C.RELAY_SECRET}" in param         # the per-process secret on our publish URL ...
                      and (C.OWN_IP is None or body.get("ip") == C.OWN_IP))   # ... and the publisher is this container
            else:
                ok = False
        elif path == "/srs/edge_on_publish":                     # edge: outsiders may publish APP_IN with the token
            q = urllib.parse.parse_qs(param.lstrip("?"))
            supplied = (q.get("token") or [""])[0]
            ok = (app == C.APP_IN and C.name_ok(stream) and bool(C.TOKEN["publish"])
                  and re.fullmatch(C.TOKEN_RE, supplied) is not None            # compare_digest needs ASCII
                  and hmac.compare_digest(supplied.encode(), C.TOKEN["publish"].encode()))
        else:                                                    # edge: outsiders may play APP_OUT, nothing else
            ok = app == C.APP_OUT and C.name_ok(stream)
        if not ok and time.monotonic() - _deny_logged_at[0] > 10:   # throttled: the public port is not a log-spam vector
            _deny_logged_at[0] = time.monotonic()
            log(f"{path.rsplit('/', 1)[1]} denied for {app[:32]}/{stream[:64]} from {body.get('ip', '?')}")
        self._send(200 if ok else 403, {"code": 0 if ok else 1})

    # --- DELETE -------------------------------------------------------------------------------
    def do_DELETE(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self._path() != "/state":
            return self._send(404, {"error": "not found"})
        try:
            os.remove(C.STATE_FILE)
        except FileNotFoundError:
            pass
        except Exception as e:
            return self._send(500, {"error": str(e)})
        with S.relays_lock:
            C.reset_overrides()
        self._send(200, {"state": "cleared",
                         "note": "publish token reset to env now; env delays apply to streams that (re)start; "
                                 "default delay re-applies on relay restart"})
