# V6 — `docker compose up` brings the whole system live (compose track)

Track owner: `docker/**`, `docker-compose.yml`, `scripts/compose-smoke.sh`,
`scripts/local-up.sh`, `emulator/serve.py`, `tests/test_compose_*.py`, this file.

**Status 2026-09-24: written correct-by-construction and validated without
Docker. V6 was NOT executed with Docker here — Docker is not installed on this
machine** (`which docker` finds nothing; `scripts/compose-smoke.sh` exits 2 with
that message). What *was* run: the identical topology on the venv via
`scripts/local-up.sh` (both services healthy in **0.94 s**, the end-to-end job
included in **6.60 s**), the emulator and mock cloud spawned individually under
pytest, a Bumble central attached to the emulator over TCP and receiving the
portVersion-7 advertisement, and static checks that every path, port and
endpoint the compose file names is real. The one thing only Docker can prove —
that the images build and `docker compose up -d --wait` reaches healthy in
under 60 s — remains to be run by someone with Docker (`./scripts/compose-smoke.sh`).

## 1. Topology

```
                    docker network "plaud-harness" (bridge, internal: true — no route out)
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │  emulator  (emulator/serve.py)                       mockcloud  (python -m mockcloud) │
 │  ┌──────────────────────────────────────────────┐    ┌──────────────────────────────┐ │
 │  │ PlaudPeripheral (portVersion 7, cleartext)   │    │ FastAPI partner-cloud mock   │ │
 │  │   └─Host─ Controller "plaud"                 │    │ GET /_mock/health  :8787     │ │
 │  │            ║ LocalLink (Bumble virtual link) │    │ --local-file-root /app/build │ │
 │  │           Controller "central" ─HCI─► :9000  │    └──────────────────────────────┘ │
 │  │ health JSON line ───────────────────► :9001  │                 ▲                   │
 │  └──────────────────────────────────────────────┘                 │                   │
 │                    ▲ tcp-client:emulator:9000                     │ http://mockcloud:8787
 │  job  (docker/job.sh, profile "check", depends_on both service_healthy)               │
 │    probe mockcloud ─ probe emulator ─ scan (attach a central, see the advertisement)  │
 │    ─ generator batch (2 × smoke) ─ pipeline batch --pipeline oracle ─ evals batch     │
 │      --suite oracle  → exit 0 only if every gate passes                               │
 │                                                                                       │
 │  named volume plaud-harness-build → /app/build in all three (job output, mock         │
 │  persistence, meeting dirs the emulator may serve via PLAUD_MEETING_DIR)              │
 └──────────────────────────────────────────────────────────────────────────────────────┘
```

Why the emulator exposes a *controller* and not the peripheral's own host: Bumble
transports carry HCI between a host and a controller, so whatever dials the TCP
port must play the other role. A central is a host; it therefore needs a
controller on the port. `emulator/serve.py` runs a `LocalLink` with two virtual
controllers — the peripheral's, and one whose HCI is served on
`PLAUD_HCI_TRANSPORT` (`tcp-server:0.0.0.0:9000`). Any Bumble host that opens
`tcp-client:<emulator>:9000` gets a controller on that link and can scan,
connect and pull the recording, radio-free. `PLAUD_HCI_MODE=direct` keeps the R7
rig instead (the peripheral's host directly on the transport, e.g.
`android-netsim`), which nothing can *attach to* but which the Android emulator
can dial into.

The emulator serves `tests/fixtures/r6s2_16k_mono.ogg` (11 271 bytes, the R6-S2
synthetic fixture, `not_plaud_capture`) unless `PLAUD_MEETING_DIR` names a
`plaud-harness/meeting/1` directory, in which case it serves
`<dir>/device/recording.ogg`; a set-but-missing path is a configuration error
(exit 2), never a silent fallback. Transfers run from a cancellable task with
4 ms inter-frame pacing (`stream_in_task=True, response_pacing_s=0.004`), the
R7-S13 policy under which the genuine SDK pulled the file byte-exact.

| service | image (built locally) | entrypoint | ports (internal) | healthcheck |
|---|---|---|---|---|
| `emulator` | `docker/Dockerfile.emulator` | `python /app/emulator/serve.py` | 9000 HCI (tcp-server), 9001 health | one JSON line from 127.0.0.1:9001 must have `"ready": true` |
| `mockcloud` | `docker/Dockerfile.mockcloud` | `python -m mockcloud --host 0.0.0.0 --port 8787 --local-file-root /app/build` | 8787 | `GET http://127.0.0.1:8787/_mock/health` is 200 and `{"mock": true}` |
| `job` | `docker/Dockerfile.job` | `bash /app/docker/job.sh` | — | none (one-shot; its exit code is the verdict) |

Healthchecks: `interval 2s, timeout 3s, retries 30, start_period 5s`, stdlib-only
python one-liners so the images need nothing extra. The emulator's health
listener is a **separate port on purpose**: Bumble's `tcp-server` transport
binds its packet sink to the most recent client, so a healthcheck that
connected to the HCI port would detach a central that was already attached.

## 2. Running with Docker

```bash
docker compose build                                  # one-time; minutes (numpy/scipy/pyroomacoustics/av)
docker compose up -d --wait                           # the system: emulator + mockcloud healthy
docker compose run --rm job                           # the end-to-end check; exit code = verdict
docker compose logs emulator mockcloud
docker compose down -v                                # also drops the plaud-harness-build volume

./scripts/compose-smoke.sh                            # build (untimed) → time `up -d --wait` → run job → down -v
KEEP_UP=1 SKIP_BUILD=1 ./scripts/compose-smoke.sh     # reuse images, leave it running
```

`compose-smoke.sh` prints `up_wait_s=<n> target_s=60` and fails if the bring-up
exceeds the target, `up --wait` fails, or the job exits non-zero; it exits 2 when
Docker, the compose v2 plugin or the daemon is missing. The image build is
excluded from the timed window (HARNESS_POLICY: V6 is about bringing the system
live; the first pip install dominates by minutes and is printed separately).

The job is behind the `check` profile so that plain `docker compose up -d --wait`
is well defined on every compose version: `--wait` treats a one-shot service
that exits 0 inconsistently across releases (`ServiceConditionRunningOrHealthy`
vs `service_completed_successfully`), and the job's `depends_on … service_healthy`
is honoured by `docker compose run` and `--profile check up` alike.

Reaching the services from the host (the default network is sealed, and Docker
cannot publish ports from an internal network):

```bash
docker compose -f docker-compose.yml -f docker/compose.host-ports.yml up -d --wait
python docker/probe.py scan tcp-client:127.0.0.1:9000      # a central sees the advertisement
curl http://127.0.0.1:8787/_mock/health ; curl http://127.0.0.1:9001/
```

Serving a generated meeting instead of the fixture (after the job has written
one into the volume):

```bash
docker compose run --rm job
PLAUD_MEETING_DIR=/app/build/compose/job/meetings/synth-smoke-s0001 docker compose up -d emulator
```

### Bumble inside the images

The harness installs Bumble editable from the immutable reference tree, so the
images do the same from the **build-context copy** of `reference/upstream/bumble`
at the commit pinned in `docs/reference-pins.txt`
(`d371a27ebd8ab87b871a542df8442718b7882fdb`; `ARG BUMBLE_PIN` in every
Dockerfile, cross-checked by `tests/test_compose_topology.py`). The copy carries
no `.git` (excluded by the ignore files; setuptools_scm would also need `git` in
the image), so the version string the venv reports —
`0.1.dev1+gd371a27eb` — is handed to setuptools_scm via
`SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BUMBLE`. The alternative (clone the pin at
build time; needs git and network during the build only) is kept as a comment in
the Dockerfiles. Nothing under `reference/**` is modified by any of this.

The three Dockerfiles share one prologue verbatim (base, pip layers, package
copies) so BuildKit shares the layers; only the last lines differ. Each has a
sibling `Dockerfile.<svc>.dockerignore` (BuildKit reads it next to the
Dockerfile) that keeps the 495 MB venv, `build/`, `data/`, the Android rig and
every reference tree except bumble out of the context.

## 3. Running without Docker

```bash
./scripts/local-up.sh
EMULATOR_HCI_PORT=19000 EMULATOR_HEALTH_PORT=19001 MOCKCLOUD_PORT=18787 ./scripts/local-up.sh
```

The script starts `emulator/serve.py` and `python -m mockcloud` from the venv in
the background, waits on the same two predicates the compose healthchecks use
(`docker/probe.py emulator|mockcloud`), prints `LOCAL_UP_LIVE_S`, runs
`docker/job.sh` inline against `127.0.0.1`, prints `LOCAL_UP_TOTAL_S`, and tears
both processes down on exit or signal. Logs and the job output land under
`build/compose/local/` (gitignored).

**Measured on this machine (macOS 25.5, Apple Silicon, CPython 3.11.16, 2026-09-24):**

```
LOCAL_UP_LIVE_S=0.94      both services healthy
JOB_OK elapsed_s=5.59     probes + scan + 2 × smoke generation + oracle pipeline + evals (oracle suite PASS, 6 checks)
LOCAL_UP_TOTAL_S=6.60     including the job
local-up: PASS live in 0.94s (target 60s), total 6.60s including the job
```

A container adds image start-up and healthcheck cadence (2 s interval) on top of
this; the 60 s budget has roughly 50 s of headroom before the first Docker run,
which is why the claim is "correct by construction", not "measured".

The scan step's output is the emulator's actual advertisement decoded by
`plaudsim.advertising.parse_manufacturer_data`:
`address F0:1A:2B:3C:4D:5E, company 0xFFFF, port_version 7, serial 8810000001, V0001`.

## 4. Probes and attaching your own central

`docker/probe.py` is the healthchecks' twin plus one thing a one-liner cannot
do; each subcommand polls until success or `--timeout` and prints one
`PROBE_OK`/`PROBE_FAIL` line:

```
python docker/probe.py tcp        HOST:PORT
python docker/probe.py emulator   HOST:PORT        # health line ready:true (prints it)
python docker/probe.py mockcloud  URL              # GET URL/_mock/health
python docker/probe.py scan       tcp-client:HOST:9000 [--address F0:1A:2B:3C:4D:5E]
```

Your own Bumble host attaches the same way the probe does:

```python
async with await open_transport("tcp-client:127.0.0.1:9000") as hci:
    device = Device.with_hci("central", Address("F0:F1:F2:F3:F4:F6"), hci.source, hci.sink)
    await device.power_on(); await device.start_scanning()   # then connect(F0:1A:2B:3C:4D:5E) and use plaudsim
```

## 5. HARNESS_POLICY register (choices, not evidence)

1. Bridge mode: the peripheral on an in-process virtual controller, a second virtual controller served on `PLAUD_HCI_TRANSPORT`; `direct` mode kept for the R7 rig.
2. Defaults: `tcp-server:0.0.0.0:9000`, health `0.0.0.0:9001` (JSON line; HTTP-shaped reply if the client sends `GET`), device `PlaudNotePro` / `F0:1A:2B:3C:4D:5E`, bridge controller `F0:F1:F2:F3:F4:F5`, probe central `F0:F1:F2:F3:F4:F6`, manufacturer company id `0xFFFF`, session id `1700000000`, file-table entry `scene 2 / attribute 1` (all from `r7/pull_capture_peripheral.py`).
3. Served file precedence `PLAUD_FILE` > `PLAUD_MEETING_DIR/device/recording.ogg` > R6-S2 fixture; set-but-missing is exit 2.
4. `stream_in_task=True, response_pacing_s=0.004` (R7-S13).
5. Health on a separate port (tcp-server single-client sink); health `interval 2s / timeout 3s / retries 30 / start_period 5s`; no `start_interval` (needs Engine 25+).
6. Job: `generator batch --n 2 --scenario smoke --seed 1`, `pipeline batch --pipeline oracle --fail-fast`, `evals batch --suite oracle`; output under `/app/build/compose/job`; probe timeout 30 s; the oracle is a harness self-test, not a system under test (its score says the wiring works, nothing about ASR).
7. Job behind profile `check`; `restart: "no"`; `--wait-timeout 2 × target` in the smoke script; build excluded from the timed window.
8. Network `plaud-harness`: bridge, `internal: true`, nothing published; `docker/compose.host-ports.yml` publishes on `127.0.0.1` only. No secrets, no `env_file`, no external network, no image beyond `python:3.11-slim`.
9. Named volume `plaud-harness-build` at `/app/build`; mockcloud `--local-file-root /app/build`.
10. Images: `python:3.11-slim`; `pip install -e` of the context copy of bumble with a pretend setuptools_scm version; `requirements/all.txt`; `PYTHONPATH=/app:/app/emulator`; per-Dockerfile ignore files kept identical.
11. Container mockcloud binds `0.0.0.0` (the module default `127.0.0.1` is unreachable from other containers).

## 6. Tests

`tests/test_compose_topology.py` (26): compose YAML parses; the three services;
job depends on both with `service_healthy`, profile `check`, `restart: no`; named
volume mounted by all; network internal, nothing published, no `network_mode`/
`env_file`/`secrets`; every build context, Dockerfile, `COPY` source and `CMD`
path exists; mockcloud binds `0.0.0.0`; Dockerfiles carry the bumble pin from
`docs/reference-pins.txt`; the three ignore files are identical and exclude the
right trees; the host-ports override binds loopback only; the emulator
healthcheck targets `serve.DEFAULT_HEALTH_PORT` and the env matches
`serve.py`'s defaults; the mockcloud healthcheck's path is a route
`create_app()` really registers and its port matches the command; healthcheck
one-liners are valid Python; job env points at the service names, an existing
gate suite and an existing generator preset; scripts are executable and pass
`bash -n`; `compose-smoke.sh` exits 2 with a clear message when Docker is
absent; `serve.py` recording resolution order and config defaults; this document
states V6 was not executed with Docker.

`tests/test_compose_runtime.py` (5): `serve.py` opens the health and HCI ports
within 20 s, its status carries the policy values, the health port answers HTTP
too, a central attached via `tcp-client` receives the advertisement decoded to
portVersion 7; exit 2 on a missing meeting dir; serving a generated meeting's
`device/recording.ogg`; `python -m mockcloud` answers `/_mock/health` within
20 s; `scripts/local-up.sh` completes within 60 s with `JOB_OK` and a
`plaud-harness/eval-report/1` report. Ephemeral ports throughout; tests skip
with a reason if processes cannot be spawned.

```
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_compose_topology.py tests/test_compose_runtime.py -q -p no:cacheprovider --timeout=120
```

## 7. Not done, and why

* **V6 itself with Docker.** Not installed here. `docker compose build` and
  `up -d --wait` have never run; the Dockerfiles, ignore files and compose file
  are checked by parsing and path/endpoint existence only. Unverified in
  particular: that every pin in `requirements/all.txt` has a manylinux wheel for
  the build platform (`pyroomacoustics`, `meeteval`, `av` build from source
  without a compiler otherwise — add `build-essential` to the prologue if pip
  reports so), the actual image build time, and the compose version's
  `--wait`/`--wait-timeout` behaviour.
* **Cloud round trip in the job.** The job proves the mock is reachable and
  serving; it does not push the generated `device/recording.ogg` through
  presign → PUT → complete → transcribe and score the mock's ground-truth
  result (`mockcloud.oracle.result_to_hypothesis`). That is the natural next
  step and needs nothing new in the images.
* **Real central in the stack.** No container runs the Android template or
  netsim; the "central" in the job is the Bumble probe. `PLAUD_HCI_MODE=direct`
  plus the R7 rig is how a real SDK is attached.
* **TLS** in front of mockcloud for the template app (docs/mockcloud.md caveat)
  is not part of the topology.
* **Timing under Docker** is extrapolated from the 0.94 s / 6.60 s venv run,
  not measured.
