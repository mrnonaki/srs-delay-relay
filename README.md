# srs-delay-relay

A delayed public copy of every live stream on an [SRS](https://github.com/ossrs/srs) server, with a
token-gated RTMP ingest for outsiders. Three plain Docker Compose services, no re-encoding, no Docker
socket.

```
encoder (SRT 10080 / RTMP+token via edge)  →  srs  app source/   ← control room pulls here, no delay
                                                 │
                                           delay-relay  (ffmpeg -c copy → RAM buffer N s → ffmpeg -c copy)
                                                 │
                                              srs  app live/     ← edge pulls here
                                                 │
                                      edge (SRS in edge mode, the only forwarded port)  ← outsiders play, no token
```

| service       | role                                                                                      |
|---------------|-------------------------------------------------------------------------------------------|
| `srs`         | ossrs/srs:6, env-only config tuned for low delay, LAN ports 1985 (API) 8080 (HTTP-FLV) 10080/udp (SRT) |
| `delay-relay` | mirrors `source/<name>` → `live/<name>` with the configured delay; control API on 9090 (LAN only); answers the edge's token check |
| `edge`        | a second SRS in edge mode (forwards publishes to, pulls plays from, `srs`): publish `rtmp://public/source/<name>?token=…`, play `rtmp://public/live/<name>`; the relay's hooks allow nothing else |

## Run

```sh
cp .env.example .env        # set PUBLISH_TOKEN, CONTROL_TOKEN (8-128 chars [A-Za-z0-9_.-]) and LAN_BIND
docker compose up -d        # podman compose (docker-compose provider) works too
```

Forward TCP `EDGE_PORT` (1935) and nothing else. The control API (9090) and the SRS ports (1985, 8080 =
undelayed source, 10080/udp = SRT ingest, no token) bind to `LAN_BIND`: your LAN/VPN address, or `0.0.0.0`
only on a host that has no public interface. `CONTROL_TOKEN` is required: the control API can rotate the
publish token and change delays. `DELAY_SECONDS` is the delay of the public copy; `DELAY_MAP=stage1=5,stage2=30`
overrides it per stream. SRS asks the relay before accepting any publish, so nothing but the relay itself can
publish into `live/` — an SRT encoder cannot skip the token or the delay.

## Control API (`:9090`, JSON; `Authorization: Bearer <CONTROL_TOKEN>` or `?auth=` on everything but the hooks)

| call | effect |
|------|--------|
| `GET /health` | relays, SRS reachability (open) |
| `POST /srs/edge_on_publish`, `POST /srs/edge_on_play` | the edge's hooks: publish `source/` with the token, play `live/`, deny the rest (open) |
| `GET /streams` | per relay: state, delay, buffered seconds, bytes in/out |
| `PUT /delay/<name>?seconds=N` | change one stream live (decrease: the excess is flushed in a burst, new viewers get the shorter delay, connected players keep buffering; increase ≤ 3 s: short pause; larger: buffer plays out, relay restarts with the new delay) |
| `PUT /delay?seconds=N[&apply=all]` | default for new streams; `apply=all` pins N on every running stream |
| `PUT /edge/token?value=NEW` | rotate the publish token at once, no restart (kept in the state volume, wins over `.env` until `DELETE /state`) |
| `GET /srs/srt-latency`, `PUT …?ms=N` | SRT latency; written to `config/srt.env`, applied by `scripts/apply-srt.sh` (cron: recreates `srs` whenever its running value differs from the file) or `docker compose up -d srs` |
| `DELETE /state` | forget API-made overrides |
| `POST /srs/on_publish` | the origin's publish hook: encoders may publish `source/`, only the relay may publish `live/` (open) |

## Behaviour worth knowing

- When a source stops, its buffered tail is played out to viewers, not dropped. A source that comes back
  gets a new relay immediately; it starts publishing only after the old one finished.
- On `docker compose stop` (or `restart delay-relay`) the relay plays out its buffers first while srs and the
  edge keep serving viewers (the relay is stopped before them); raise `STOP_GRACE_SECONDS` above your longest
  delay + 15 s or the play-out is cut short.
- After the API has written `config/srt.env` the file is owned by root (the relay runs as root in its
  container); edit it with sudo or through the API. It is tracked in git, so a deployed checkout shows it as
  modified (`git update-index --skip-worktree config/srt.env` if that bothers you).
- While delay-relay is down or restarting, neither SRS accepts new publishes or new outside viewers (their hooks
  have nobody to ask); connected publishers and viewers continue.
- Codecs: H.264 (tested over RTMP) and H.265 (tested over SRT ingest), AAC audio. The delayed copy is
  delivered as enhanced RTMP: players need ffmpeg ≥ 6.1 or OBS ≥ 30. SRS 6 (the default, stable line) writes
  H.265 into FLV as legacy codec id 12; the relay image (Alpine 3.24, ffmpeg 8.1.2) reads that, so the relay
  pulls every codec over HTTP-FLV on SRS 6 and 7 alike. Built with an older ffmpeg it falls back to SRT for
  H.265 on SRS 6, which adds SRS's SRT latency (~250 ms) to the delay (`PULL=srt|flv` forces a path; `/health`
  shows the ffmpeg version and mode). Anything else reading `source/` H.265 straight from SRS 6 needs ffmpeg ≥ 7.1
  (OBS ≥ 31). `SRS_IMAGE=docker.io/ossrs/srs:7` (develop line) emits enhanced RTMP everywhere; both are tested.
- Delays are bounded by `MAX_DELAY_SECONDS` and all buffers together by `MAX_BUFFER_MB` (RAM per stream
  ≈ bitrate × delay). Keep `RELAY_MEM_LIMIT` above that.
- If only the publishing ffmpeg dies (SRS idle-dropped it) the relay restarts and the buffered seconds are lost once.
- When the total buffer exceeds `MAX_BUFFER_MB` the relay holding the most drops its oldest data (a visible skip) rather than refusing streams.
- Both SRS instances drop a dead publisher after 5 s (`publish normal_timeout`), so an encoder reconnecting on
  the same key is accepted almost at once.
- `SRS_VHOST_PLAY_MW_LATENCY` must stay `1`, not `0`: 0 makes SRS spin at 100 % CPU with any HTTP-FLV consumer.

## What it is not

- No TLS: the publish token travels inside the RTMP URL in clear text. Use a VPN or accept it.
- SRS logs the body of a *denied* publish hook at ERROR level, including whatever token that client sent; the
  real token lands in the edge's log only if a legitimate publish is denied while delay-relay is unreachable.
- No rate limiting on the public port (a host firewall can add per-IP limits).
- No HLS/WebRTC/SRT output, no recording, no transcoding, no multi-host. Playback for outsiders is RTMP only.
- The delay is N seconds plus a few hundred ms of probing; a source that drops and returns costs viewers
  nothing, but a relay whose publisher is dropped by SRS loses its buffered seconds once.
- RAM only: `MAX_STREAMS` includes relays still playing out; per stream RAM ≈ bitrate × delay.

## Layout

```
app/            config.py (env, state.json, srt.env) · relay.py (one stream) · supervisor.py (which relays exist)
                api.py (control API, /auth, /srs/on_publish) · main.py (startup, SIGTERM play-out)
config/srt.env  SRT latency (compose env_file for srs; rewritten by the API)
edge/srs-edge.conf  the edge's SRS config (cluster mode + hooks; no secrets)
scripts/        apply-srt.sh — cron/systemd applier for latency changes
tests/e2e.py    end-to-end test (builds and runs the whole stack)
.env.example    every setting, with comments
```

## Test

```sh
python3 tests/e2e.py                    # or: COMPOSE="podman compose" python3 tests/e2e.py
```

Builds the images, runs the stack under project `drtest` on test ports, exercises publish/play/token/latency/restart/
dropout/12-source/H.265 scenarios from a helper container, tears down. About 8 minutes; exit code 1 on any failure.
