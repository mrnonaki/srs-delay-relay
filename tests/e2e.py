#!/usr/bin/env python3
"""End-to-end test of the 3-service compose stack. Needs docker compose v2 (or podman with the docker-compose
provider: COMPOSE="podman compose"). Builds the images, brings the stack up as project 'drtest' on its own ports
(edge 21935, control 29090, SRS 21985/28080/20080) and subnet 172.30.78.0/24, drives ffmpeg publishers and players
from a helper container on the compose network, then tears everything down. Exit code 1 when any check fails.
About 8 minutes."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

D = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = "drtest"
LAB = f"{P}-lab"
CFG = os.path.join(D, "config-test")
SRTF = os.path.join(CFG, "srt.env")
COMPOSE = shlex.split(os.environ.get("COMPOSE", "docker compose"))
ENGINE = "podman" if "podman" in COMPOSE[0] else "docker"
E = dict(os.environ, EDGE_PORT="21935", CONTROL_PORT="29090", SRS_API_PORT="21985", SRS_HTTP_PORT="28080",
         SRS_SRT_PORT="20080", LAN_BIND="127.0.0.1", SUBNET_PREFIX="172.30.78", PUBLISH_TOKEN="topoTOKEN1",
         CONTROL_TOKEN="ctl", POLL_SECONDS="2", DELAY_SECONDS="2", DELAY_MAP="", APP_IN="source", APP_OUT="live",
         STOP_GRACE_SECONDS="60", MAX_PAUSE_SECONDS="3", CONFIG_DIR=CFG, COMPOSE_PROJECT_NAME=P)
API = "http://127.0.0.1:29090"
EDGE = "rtmp://edge:1935"
SRT_KEYS = ("SRS_SRT_SERVER_LATENCY", "SRS_SRT_SERVER_RECVLATENCY", "SRS_SRT_SERVER_PEERLATENCY")
failures = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        failures.append(label)


def sh(*a, **k):
    return subprocess.run(a, capture_output=True, text=True, **k)


def compose(*a):
    return sh(*COMPOSE, *a, cwd=D, env=E)


def api(method, path, auth=True):
    req = urllib.request.Request(API + path, method=method)
    if auth:
        req.add_header("Authorization", "Bearer ctl")
    try:
        with urllib.request.urlopen(req, timeout=60) as x:
            return json.loads(x.read())
    except urllib.error.HTTPError as e:
        return {"HTTP": e.code}
    except Exception as e:
        return {"err": str(e)[:80]}


def lab(*a, **k):
    return sh(ENGINE, "exec", LAB, *a, **k)


def ff(timeout_s, *args):
    return lab("timeout", str(timeout_s), "ffmpeg", "-hide_banner", "-loglevel", "error", *args)


def play(url):
    return ff(9, "-i", url, "-t", "2", "-f", "null", "-").returncode == 0


def pub_try(url, muxer="flv", codec="libx264"):
    return ff(8, "-re", "-f", "lavfi", "-i", "color=c=black:s=160x90:r=10", "-t", "3", "-c:v", codec, "-preset", "ultrafast",
              "-g", "10", "-f", muxer, url).returncode == 0


def pub_loop(name, token, secs=600):
    cmd = (f"for i in $(seq 1 40); do timeout {secs} ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc2=s=320x180:r=30 "
           f"-c:v libx264 -preset ultrafast -g 30 -b:v 400k -maxrate 400k -bufsize 400k -f flv "
           f"'{EDGE}/source/{name}?token={token}'; sleep 2; done")
    return subprocess.Popen([ENGINE, "exec", LAB, "sh", "-c", cmd], stderr=subprocess.DEVNULL)


def relays():
    return [(x["name"], x["state"], x["uptime_s"]) for x in api("GET", "/streams").get("streams", [])]


def c(name):
    return f"{P}-{name}-1"


def started_at(name):
    return sh(ENGINE, "inspect", "-f", "{{.State.StartedAt}}", c(name)).stdout.strip()


def logs(name):
    r = sh(ENGINE, "logs", c(name))
    return r.stdout + r.stderr


def kill_pubs():
    lab("sh", "-c", "pkill -f '[s]eq 1 40'; pkill -x ffmpeg; true")       # [s]eq: never match this shell itself


def srs_streams():
    out = lab("sh", "-c", "wget -qO- 'http://srs:1985/api/v1/streams/?count=100'").stdout
    try:
        return {(s["app"], s["name"]) for s in json.loads(out)["streams"] if s.get("publish", {}).get("active")}
    except Exception:
        return set()


def cleanup(pub):
    if pub:
        pub.kill()
    kill_pubs()
    sh(ENGINE, "rm", "-f", LAB)
    compose("down", "-v")
    shutil.rmtree(CFG, ignore_errors=True)


def main():
    pub = None
    sh(ENGINE, "rm", "-f", LAB)
    os.makedirs(CFG, exist_ok=True)
    open(SRTF, "w").write("".join(f"{k}=250\n" for k in SRT_KEYS))
    compose("down", "-v", "--remove-orphans")
    r = compose("up", "-d", "--build")
    if r.returncode != 0:
        print("compose up failed:", r.stderr.strip()[-800:])
        cleanup(None)
        sys.exit(2)
    try:
        imgs = [i for i in sh(ENGINE, "images", "--format", "{{.Repository}}").stdout.split() if i.endswith(f"{P}-delay-relay")]
        check(bool(imgs), "relay image built")
        if not imgs:
            raise SystemExit(1)
        r = sh(ENGINE, "run", "-d", "--name", LAB, "--network", f"{P}_default", "--entrypoint", "sleep", imgs[0], "infinity")
        check(r.returncode == 0, "helper container on the compose network")
        for _ in range(45):
            time.sleep(2)
            if api("GET", "/health").get("srs_api_ok"):
                break
        check(api("GET", "/health", auth=False).get("srs_api_ok"), "relay up and SRS API reachable")
        ips = sh(ENGINE, "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", c("srs"), c("delay-relay")).stdout.split()
        check(ips == ["172.30.78.10", "172.30.78.11"], f"static IPs srs/relay {ips}")

        pub = pub_loop("t1", "topoTOKEN1")
        time.sleep(20)
        check(pub.poll() is None and any(n == "t1" for n, _, _ in relays()), f"1 publish via edge with token -> relay {relays()}")
        check(play(f"{EDGE}/live/t1"), "2 play live/t1 via edge")
        check(not play(f"{EDGE}/source/t1?token=topoTOKEN1"), "3 play source/ via edge denied")
        check(not pub_try(f"{EDGE}/source/x1?token=wrongTOKEN"), "4a publish with wrong token denied")
        check(not pub_try(f"{EDGE}/source/x2"), "4b publish without token denied")
        check(not pub_try(f"{EDGE}/live/x3?token=topoTOKEN1"), "4c publish into live/ via edge denied")
        check(not pub_try(f"{EDGE}/source/x%C3%A9?token=topoTOKEN1"), "4d publish with non-ASCII name denied, API alive: "
              + str(api("GET", "/health", auth=False).get("ok")))

        e0 = started_at("edge")
        check(api("PUT", "/edge/token?value=tokBBBBB").get("token_len") == 8, "5 rotate token")
        check(pub_try(f"{EDGE}/source/x4?token=tokBBBBB"), "5a new token accepted")
        check(not pub_try(f"{EDGE}/source/x5?token=topoTOKEN1"), "5b old token denied")
        check(started_at("edge") == e0, "5c edge not restarted by the rotation")
        check(api("PUT", "/edge/token?value=change-me-publish") == {"HTTP": 400}, "5d placeholder token rejected")
        check(api("PUT", "/edge/token?value=abcdefgh%0A") == {"HTTP": 400}, "5e newline token rejected")

        check(api("PUT", "/srs/srt-latency?ms=600").get("status") == "pending", "6 PUT srt-latency 600 -> pending")
        check("600" in open(SRTF).read(), "6a srt.env rewritten")
        check("tokBBBBB" not in logs("edge"), "6b edge log never shows the current token (denied attempts are logged by SRS)")
        check(api("GET", "/srs/srt-latency").get("status", "").startswith("pending"), "6c GET shows pending before apply")
        env_apply = dict(E, COMPOSE=" ".join(COMPOSE))
        r = sh("sh", os.path.join(D, "scripts", "apply-srt.sh"), env=env_apply)
        check(r.returncode == 0 and "600" in r.stdout, f"6d apply-srt.sh recreated srs ({r.stdout.strip()[:60]})")
        r2 = sh("sh", os.path.join(D, "scripts", "apply-srt.sh"), env=env_apply)
        check(r2.returncode == 0 and r2.stdout.strip() == "", "6e apply-srt.sh second run is a no-op")
        time.sleep(6)
        env = sh(ENGINE, "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", c("srs")).stdout.split()
        check("SRS_SRT_SERVER_LATENCY=600" in env, "6f srs runs with latency 600")
        check(api("GET", "/srs/srt-latency").get("status", "").startswith("SRS restarted"), "6g GET shows SRS restarted since the change")

        pub.kill()
        kill_pubs()
        time.sleep(3)
        t6 = time.time()
        for _ in range(20):                      # same key, new publisher: the edge must drop the dead session fast
            if pub_try(f"{EDGE}/source/t1?token=tokBBBBB"):
                break
        check(time.time() - t6 < 12, f"6h same-key republish accepted after {time.time()-t6:.0f}s")
        pub = pub_loop("t1", "tokBBBBB")
        time.sleep(20)
        check(play(f"{EDGE}/live/t1") and started_at("edge") == e0, "6i play after SRS recreate, edge untouched (static IP)")

        t7 = time.time()
        sh(ENGINE, "restart", "-t", "1", c("srs"))
        time.sleep(10)
        ok = False
        for _ in range(4):
            ok = play(f"{EDGE}/live/t1")
            if ok:
                break
            time.sleep(5)
        check(ok, f"7 play recovers after an external SRS restart ({time.time()-t7:.0f}s)")

        sh(ENGINE, "restart", "-t", "20", c("delay-relay"))
        time.sleep(12)
        ok = False
        for _ in range(4):
            ok = play(f"{EDGE}/live/t1")
            if ok:
                break
            time.sleep(5)
        check(ok, "8 play recovers after a relay restart")
        check(any("shutdown: playing out" in l for l in logs("delay-relay").splitlines()), "8a relay played out its buffer on SIGTERM")

        r8 = api("PUT", "/delay/t1?seconds=8")
        check(r8.get("applied_now") is False and "restart" in (r8.get("note") or ""), f"8b delay 2 -> 8 = restart path announced {r8}")
        time.sleep(16)
        lab("sh", "-c", "pkill -x ffmpeg")           # only the publisher process dies; its loop republishes 2 s later
        ok = False
        for _ in range(8):
            time.sleep(1)
            st = relays()
            ok = (len([1 for n, s, _ in st if n == "t1" and s == "draining"]) == 1
                  and len([1 for n, s, _ in st if n == "t1" and s in ("starting", "running")]) == 1)
            if ok:
                break
        check(ok, f"8c dropout: predecessor draining + successor started {st}")
        t8 = time.time()
        ok = False
        while time.time() - t8 < 40 and not ok:
            ok = play(f"{EDGE}/live/t1")
        check(ok, f"8d successor publishing {time.time()-t8+3.5:.0f}s after the dropout")
        el = logs("edge")
        check("already publishing" not in el and "StreamBusy" not in el, "8e edge never saw a second publisher on the same key")

        check(api("GET", "/streams", auth=False) == {"HTTP": 401}, "9 control API needs the token")
        check(api("GET", "/edge").get("token_source") == "state.json", "9a rotated token persisted in state")
        lab("sh", "-c", "pkill -f '[f]fmpeg.*source/t1'")
        time.sleep(1.0)
        st = [(x["name"], x["state"]) for x in api("GET", "/streams")["streams"]]
        check(("t1", "draining") in st, f"10 source gone -> relay draining {st}")
        pub.kill()
        kill_pubs()
        time.sleep(6)

        for i in range(1, 13):
            subprocess.Popen([ENGINE, "exec", LAB, "sh", "-c",
                              f"timeout 40 ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc2=s=160x90:r=10 -c:v libx264 "
                              f"-preset ultrafast -g 10 -b:v 150k -f flv '{EDGE}/source/n{i}?token=tokBBBBB'"], stderr=subprocess.DEVNULL)
        n = []
        for _ in range(30):
            time.sleep(1)
            n = sorted(x["name"] for x in api("GET", "/streams")["streams"])
            if len(n) == 12:
                break
        check(len(n) == 12, f"11 twelve sources -> twelve relays ({len(n)})")
        kill_pubs()
        time.sleep(4)

        srt = "srt://srs:10080?streamid=#!::r=source/h1,m=publish&latency=120000"
        subprocess.Popen([ENGINE, "exec", LAB, "sh", "-c",
                          f"timeout 60 ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc2=s=320x180:r=30 -c:v libx265 "
                          f"-preset ultrafast -g 30 -b:v 400k -f mpegts '{srt}'"], stderr=subprocess.DEVNULL)
        time.sleep(18)
        codec = lab("timeout", "15", "ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name",
                    "-of", "csv=p=0", f"{EDGE}/live/h1").stdout.strip()
        check(codec == "hevc" and play(f"{EDGE}/live/h1"), f"12 H.265 over SRT -> relay -> edge plays as {codec or 'nothing'}")
        first = subprocess.Popen([ENGINE, "exec", LAB, "timeout", "12", "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", f"{EDGE}/live/h1",
                                  "-t", "8", "-f", "null", "-"], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        time.sleep(2)
        check(play(f"{EDGE}/live/h1"), "12a a second concurrent H.265 viewer decodes too")
        first.wait()
        check(not pub_try("srt://srs:10080?streamid=#!::r=live/x9,m=publish", "mpegts"), "13 SRT publish straight into live/ denied by the SRS hook")
        check(not pub_try("rtmp://srs:1935/live/x8"), "13a RTMP publish straight into live/ on SRS denied")
        check(pub_try("rtmp://srs:1935/source/x7"), "13b RTMP publish into source/ on SRS (LAN) allowed")
        subprocess.Popen([ENGINE, "exec", LAB, "sh", "-c",            # a denied publisher that keeps retrying for 8 s
                          "timeout 8 sh -c 'while :; do ffmpeg -hide_banner -loglevel error -re -f lavfi -i color=c=black:s=160x90:r=10 "
                          "-t 8 -c:v libx264 -preset ultrafast -g 10 -f flv rtmp://srs:1935/live/x9; sleep 0.3; done'"], stderr=subprocess.DEVNULL)
        time.sleep(4)
        check(("live", "x9") not in srs_streams(), "13c nothing lands in live/ while a rogue publisher is trying")
        apis = lab("sh", "-c", "wget -qO- 'http://srs:1985/api/v1/streams/?count=100'; wget -qO- 'http://srs:1985/api/v1/clients/?count=100'").stdout
        check("relay=" not in apis, "13d relay secret not visible on the SRS API")
        kill_pubs()

        print("edge warnings:", [l[-100:] for l in logs("edge").splitlines() if "[WARN]" in l or "[ERROR]" in l][-5:] or "none")
        print("relay log tail:", [l[9:100] for l in logs("delay-relay").splitlines() if "error" in l.lower()][-5:] or "no errors")
    finally:
        cleanup(pub)
    print(f"{len(failures)} failure(s)" + (": " + "; ".join(failures) if failures else ""))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
