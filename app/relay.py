"""One Relay = one source stream, time-shifted into APP_OUT without re-encoding.

  APP_IN/<name>.flv  --ffmpeg (-c copy, mpegts)-->  RAM deque (hold N s)  --ffmpeg (-c copy, flv)-->  APP_OUT/<name>

Pacing is preserved: each chunk is released exactly N seconds after it arrived.
States: starting -> running -> draining (source gone, buffer still playing out) -> stopped.
"""
import collections
import subprocess
import threading
import time

from . import config as C
from .config import log

# RAM used by all relay buffers together; the supervisor evicts from the fattest relay when it exceeds the budget.
_budget = {"bytes": 0}
_budget_lock = threading.Lock()


def budget_add(n):
    with _budget_lock:
        _budget["bytes"] += n
        return _budget["bytes"]


def budget_bytes():
    return _budget["bytes"]


class Relay(threading.Thread):
    def __init__(self, name, delay, after=None, via_srt=False):
        super().__init__(daemon=True)
        self.name_ = name
        self.delay = C.parse_delay(delay)
        self.after = after            # predecessor still playing out APP_OUT/<name>: our first write waits for it
        self.via_srt = via_srt        # pull the source over SRT (MPEG-TS) instead of HTTP-FLV
        self.state = "starting"
        self.stop = False             # final: set by _finish after reaping, or by terminate() while it kills
        self.eof = False              # source ended
        self.ended_by_us = False      # end_source() was called (no restart backoff for a deliberate end)
        self.wr_done = False          # writer thread finished
        self.q = collections.deque()  # (arrival monotonic time, chunk)
        self.q_bytes = 0
        self.cv = threading.Condition()
        self.started = time.monotonic()
        self.drain_started = None
        self.last_rx = self.started
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.first_tx = None
        self.zero_kbps = 0            # consecutive polls on which SRS heard nothing from our publisher
        self.rd = None                # ffmpeg: source FLV -> mpegts on stdout
        self.wr = None                # ffmpeg: mpegts on stdin -> FLV publish
        self._last_drop_log = 0.0

    # --- control ------------------------------------------------------------------------------
    def buffered_s(self):
        with self.cv:
            if len(self.q) > 1:
                return round(self.q[-1][0] - self.q[0][0], 2)
            return 0.0

    def terminate(self):
        """Stop now: both ffmpegs are killed and whatever is buffered is discarded."""
        self.stop = True
        for proc in (self.rd, self.wr):
            try:
                proc.kill()
            except Exception:
                pass
        with self.cv:
            self.cv.notify_all()

    def end_source(self):
        """Stop reading the source; the buffer keeps playing out (paced) and the relay exits when it is empty."""
        self.ended_by_us = True
        try:
            self.rd.kill()
        except Exception:
            pass

    def drop_oldest(self, nbytes):
        """Global RAM budget exceeded and this relay holds the most: drop its oldest data."""
        with self.cv:
            freed = 0
            while self.q and freed < nbytes:
                freed += len(self.q.popleft()[1])
            self.q_bytes -= freed
            budget_add(-freed)
        now = time.monotonic()
        if now - self._last_drop_log > 10:
            self._last_drop_log = now
            log(f"[{self.name_}] RAM budget {C.MAX_BUFFER_BYTES >> 20} MB exceeded: dropped {freed >> 10} KB of the oldest buffered data")

    def set_delay(self, seconds):
        """Decrease: buffered excess is flushed in a burst (viewers jump forward). Increase: the output pauses
        for the difference, unless that pause would exceed MAX_PAUSE_S (SRS would drop the idle publisher)
        — then the buffer plays out with the old pacing and the supervisor restarts us with the new delay."""
        seconds = C.parse_delay(seconds)
        with self.cv:
            if self.state == "starting":          # nothing buffered yet
                self.delay = seconds
                return "live"
            if self.state != "running":           # draining/stopped: keep the old pacing, the successor gets the new value
                log(f"[{self.name_}] delay change ignored while {self.state}")
                return "ignored"
            next_release = (self.q[0][0] + seconds - time.monotonic()) if self.q else 0.0
            pause = max(seconds - self.delay, next_release)
            restart = pause > C.MAX_PAUSE_S
            if not restart:
                self.delay = seconds
            self.cv.notify_all()
        if restart:
            log(f"[{self.name_}] delay -> {seconds}s would pause output {pause:.1f}s (> {C.MAX_PAUSE_S:.0f}s): "
                f"playing out the buffer, then restarting with the new delay")
            self.end_source()
            return "restart"
        log(f"[{self.name_}] delay -> {seconds}s")
        return "live"

    # --- threads ------------------------------------------------------------------------------
    def _spawn(self):
        # via_srt: on SRS < 7 (HEVC written into FLV as legacy codec id 12, which ffmpeg cannot demux) the source
        # is pulled over SRT as plain MPEG-TS instead. Publishing always uses enhanced RTMP.
        if self.via_srt:
            src = f"{C.SRS_SRT}?streamid=#!::r={C.APP_IN}/{self.name_},m=request&latency=120000"
        else:
            src = f"{C.SRS_HTTP}/{C.APP_IN}/{self.name_}.flv"
        dst = f"{C.SRS_RTMP}/{C.APP_OUT}/{self.name_}?relay={C.RELAY_SECRET}"   # see config.RELAY_SECRET
        self.rd = subprocess.Popen(
            [C.FFMPEG, "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
             "-probesize", "200000", "-analyzeduration", "0", "-rw_timeout", "10000000",   # a stalled socket ends the read
             "-i", src, "-c", "copy", "-f", "mpegts", "-"],
            stdout=subprocess.PIPE, bufsize=0)
        self.wr = subprocess.Popen(
            [C.FFMPEG, "-hide_banner", "-loglevel", "error", "-fflags", "+nobuffer+genpts", "-flags", "low_delay",
             "-probesize", "65536", "-analyzeduration", "0", "-f", "mpegts", "-i", "-",
             "-c", "copy", "-flvflags", "no_duration_filesize", "-rw_timeout", "10000000", "-f", "flv", dst],
            stdin=subprocess.PIPE, bufsize=0)

    def _writer(self):
        """Releases chunks at arrival + delay into the publishing ffmpeg; drains the deque after EOF."""
        wr = self.wr
        try:
            while self.after and not self.after.stop and not self.stop:   # predecessor still publishing our name
                time.sleep(0.1)
            while not self.stop:
                with self.cv:
                    while not self.q and not self.stop and not self.eof:
                        self.cv.wait(0.5)
                    if self.stop or not self.q:               # empty here means: source ended and buffer drained
                        break
                    arrived, data = self.q[0]
                    wait = arrived + self.delay - time.monotonic()
                    if wait > 0:
                        self.cv.wait(min(wait, 0.5))          # woken early when the delay changes
                        continue
                    self.q.popleft()
                    self.q_bytes -= len(data)
                    budget_add(-len(data))
                wr.stdin.write(data)
                self.tx_bytes += len(data)
                if self.first_tx is None:
                    self.first_tx = time.monotonic()
            if not self.stop:
                wr.stdin.close()                              # drained: let ffmpeg end the FLV stream cleanly
                wr.wait(timeout=10)
        except (BrokenPipeError, OSError, ValueError, subprocess.TimeoutExpired):
            pass
        except Exception as e:
            log(f"[{self.name_}] writer error: {e}")
        finally:
            self.wr_done = True                               # publisher gone or drained: never leave a relay without output
            if not self.eof:
                log(f"[{self.name_}] publisher ended (SRS dropped it?), restarting")
                try:
                    self.rd.kill()                            # reader hits EOF -> run() reaps both and flips stop
                except Exception:
                    pass

    def _read_loop(self):
        rd = self.rd
        while not self.stop:
            data = rd.stdout.read(C.CHUNK)
            if not data:
                break
            with self.cv:
                now = time.monotonic()
                self.q.append((now, data))
                self.cv.notify()
                self.last_rx = now
                self.rx_bytes += len(data)
                self.q_bytes += len(data)
                budget_add(len(data))
                limit = self.delay + C.MAX_BUFFER_EXTRA_S
                if now - self.q[0][0] > limit:                # consumer stalled: drop oldest, keep memory bounded
                    dropped = 0
                    while self.q and now - self.q[0][0] > limit:
                        size = len(self.q.popleft()[1])
                        self.q_bytes -= size
                        budget_add(-size)
                        dropped += 1
                    if dropped and now - self._last_drop_log > 10:
                        self._last_drop_log = now
                        log(f"[{self.name_}] consumer stalled, dropping data (buffer > {limit:.0f}s)")

    def run(self):
        log(f"[{self.name_}] start delay={self.delay}s {C.APP_IN}/{self.name_} -> {C.APP_OUT}/{self.name_}"
            f"{' (HEVC: pulled over SRT)' if self.via_srt else ''}")
        try:
            self._spawn()
        except Exception as e:                                # ffmpeg missing / fork failure: never stay 'starting'
            log(f"[{self.name_}] cannot start ffmpeg: {e}")
            self._reap()
            self.stop = True
            self.state = "stopped"
            return
        if self.ended_by_us or self.stop:                     # end_source()/terminate() arrived while we were starting
            self.rd.kill()
        writer = threading.Thread(target=self._writer, daemon=True)
        writer.start()
        self.state = "running"
        try:
            self._read_loop()
        except Exception as e:
            log(f"[{self.name_}] reader error: {e}")
        finally:
            self._finish(writer)

    def _reap(self):
        """Kill and wait both ffmpegs (whichever exist) and close our ends of their pipes: no zombies, no fd leak."""
        for proc in (self.rd, self.wr):
            if proc is None:
                continue
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            for pipe in (proc.stdout, proc.stdin):
                try:
                    if pipe:
                        pipe.close()
                except Exception:
                    pass

    def _finish(self, writer):
        with self.cv:
            self.eof = True
            if not self.stop and not self.wr_done:
                self.drain_started = time.monotonic()
                self.state = "draining"
                buffered = (self.q[-1][0] - self.q[0][0]) if len(self.q) > 1 else 0.0
                log(f"[{self.name_}] source ended, playing out {buffered:.1f}s still buffered")
            self.cv.notify_all()
        try:
            self.rd.kill()
        except Exception:
            pass
        writer.join()                                         # returns when drained, or at once on terminate()
        self._reap()
        with self.cv:
            budget_add(-self.q_bytes)                         # whatever was not played out leaves the budget
            self.q.clear()
            self.q_bytes = 0
        self.stop = True                                      # only now may a successor publish (old publish released)
        self.state = "stopped"
        log(f"[{self.name_}] stopped")
