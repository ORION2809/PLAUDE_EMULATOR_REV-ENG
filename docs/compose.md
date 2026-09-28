# V6 — `docker compose up` brings the whole system live (compose track)

Track owner: `docker/**`, `docker-compose.yml`, `scripts/compose-smoke.sh`,
`scripts/local-up.sh`, `emulator/serve.py`, `tests/test_compose_*.py`, this file.

**Status 2026-09-25: V6 was executed under Docker once, and it passed.**
`scripts/compose-smoke.sh` ran on this Mac (Apple M1) against a Colima VM
(Linux arm64). It built the three images in 121.38 s, which is outside the
timed window by design. `docker compose up -d --wait` reached healthy in
**6.58 s** against the 60 s target, and the job exited 0 in 11.34 s
(section 2.1, `build/v6/compose-smoke-2026-09-25.log`, committed evidence). It has not
run on x86_64 or on a GitHub runner.

> **Correction, 25 September 2026.** An earlier version of this status said
> "V6 was NOT executed with Docker here", because no daemon answered at the
> time. That was true when written. A daemon was installed later that day
> (section 2.1), and the run above followed. When the Colima VM is stopped,
> `scripts/compose-smoke.sh` still exits 2 with the "daemon" message.

Before the Docker run, and still useful without a daemon:

* `docker compose config` (it needs no daemon): the file resolves, and the
  tests check the resolved names and environment (section 6);
* the identical topology on the venv via `scripts/local-up.sh`, now including
  the cloud round trip: both services healthy in **1.22 s**, the whole job in
  **9.0–9.5 s**, **10.3–10.8 s** in total (section 3);
* the emulator and the mock cloud spawned individually under pytest, including
  the review's failure cases (centrals that leave badly, a port that never
  speaks HCI, an imposter advertiser, a squatted port, a failing job, SIGTERM);
* static checks that every path, port, endpoint and pin the compose file and
  Dockerfiles name is real.

The one thing only Docker can prove — that the images build and
`docker compose up -d --wait` reaches healthy in under 60 s — was shown once on
2026-09-25, on linux/arm64 (section 2.1).

## 1. Topology

```
                  docker network "<project>_harness" (bridge, internal: true — no route out)
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │  emulator  (emulator/serve.py)                       mockcloud  (python -m mockcloud) │
 │  ┌──────────────────────────────────────────────┐    ┌──────────────────────────────┐ │
 │  │ PlaudPeripheral (portVersion 7, cleartext)   │    │ FastAPI partner-cloud mock   │ │
 │  │   └─Host─ Controller "plaud"                 │    │ GET /_mock/health  :8787     │ │
 │  │            ║ LocalLink (Bumble virtual link) │    │ --local-file-root /app/build │ │
 │  │           Controller "central-N" per client  │    │ --chunk-size 20000           │ │
 │  │             ─HCI─► :9000 (one TCP client each)│    └──────────────────────────────┘ │
 │  │ health JSON line ───────────────────► :9001  │                 ▲                   │
 │  └──────────────────────────────────────────────┘                 │                   │
 │                    ▲ tcp-client:emulator:9000                     │ http://mockcloud:8787
 │  job  (docker/job.sh, profile "check", depends_on both service_healthy)               │
 │    probe mockcloud ─ probe emulator ─ scan (attach a central, check the advertisement)│
 │    ─ generator batch (2 × smoke) ─ pipeline batch --pipeline oracle ─ evals batch     │
 │    ─ cloud round trip of meeting 1 (bind, 3-part upload, transcription) ─ evals score │
 │      --suite oracle on both  → exit 0 only if every step and gate passes              │
 │                                                                                       │
 │  named volume <project>_harness-build → /app/build in all three (job output, mock     │
 │  persistence, meeting dirs the emulator may serve via PLAUD_MEETING_DIR)              │
 └──────────────────────────────────────────────────────────────────────────────────────┘
```

`<project>` is `plaud-harness` unless `-p` / `COMPOSE_PROJECT_NAME` says
otherwise: the network and the volume carry no explicit `name:`, so two
projects never share them.

Why the emulator exposes a *controller* and not the peripheral's own host:
Bumble transports carry HCI between a host and a controller, so whatever dials
the TCP port must play the other role. A central is a host; it therefore needs
a controller on the port. `emulator/serve.py` runs a `LocalLink` with the
peripheral's controller and, **for every TCP client of `PLAUD_HCI_TRANSPORT`, a
fresh controller of its own** (HARNESS_POLICY). Any Bumble host that opens
`tcp-client:<emulator>:9000` gets a controller on that link and can scan,
connect and pull the recording, radio-free.

Why one controller per client (review finding C1): the first version handed the
port to Bumble's `tcp-server` transport with one shared controller. That
transport shares one packet parser and one sink across all clients
(`reference/upstream/bumble/bumble/transport/tcp_server.py:100-103`), and
`Controller.on_hci_reset_command` resets nothing
(`reference/upstream/bumble/bumble/controller.py:2031-2038`). A central that
closed TCP without an LE disconnect left the peripheral connected, so it never
advertised again (`controller.py:740`); one that left while scanning left
`le_scan_enable` set, so every later `LE_Set_Scan_Parameters` failed with
`COMMAND_DISALLOWED` (`controller.py:2764-2766`); one that died mid-packet
misaligned the shared parser, so the next host's `HCI_Reset` was swallowed. All
three broke the service until a restart, while health said `ready: true`. Now,
when a client's TCP connection ends, its controller sends `LL_TERMINATE_IND`
(reason 0x13) for each LE link it holds — the same PDU `on_hci_disconnect_command`
sends (`controller.py:1683-1719`) — so the peripheral sees an ordinary
disconnection and its auto-restart advertiser resumes. Scan, connect and
advertising state is cleared and the controller leaves the link. The next
client starts from a fresh controller and a fresh parser.

`PLAUD_HCI_MODE=direct` keeps the R7 rig instead (the peripheral's host
directly on the transport, e.g. `android-netsim`), which nothing can *attach
to* but which the Android emulator can dial into. A bridge-mode transport other
than `tcp-server:` (e.g. `pty:`) still gets one shared controller, without the
per-client isolation.

The emulator serves `tests/fixtures/r6s2_16k_mono.ogg` (11 271 bytes, the R6-S2
synthetic fixture, `not_plaud_capture`) unless `PLAUD_FILE` or
`PLAUD_MEETING_DIR` says otherwise. `PLAUD_MEETING_DIR` names a
`plaud-harness/meeting/1` directory, and the emulator then serves
`<dir>/device/recording.ogg`. A set-but-missing path is a configuration error
(exit 2), never a silent fallback. An empty value counts as unset: that is what
compose substitutes when the shell does not set it. Transfers run from a
cancellable task with 4 ms inter-frame pacing (`stream_in_task=True,
response_pacing_s=0.004`), the R7-S13 policy under which the genuine SDK pulled
the file byte-exact.

| service | image (built locally) | entrypoint | ports (internal) | healthcheck |
|---|---|---|---|---|
| `emulator` | `docker/Dockerfile.emulator` | `python /app/emulator/serve.py` | 9000 HCI (one controller per TCP client), 9001 health | one JSON line from 127.0.0.1:9001 must have `"ready": true` |
| `mockcloud` | `docker/Dockerfile.mockcloud` | `python -m mockcloud --host 0.0.0.0 --port 8787 --local-file-root /app/build --chunk-size 20000` | 8787 | `GET http://127.0.0.1:8787/_mock/health` is 200 and `{"mock": true}` |
| `job` | `docker/Dockerfile.job` | `bash /app/docker/job.sh` | — | none (one-shot; its exit code is the verdict) |

Healthchecks: `interval 2s, timeout 3s, retries 30, start_period 5s`, stdlib-only
python one-liners so the images need nothing extra. The emulator's health
listener is a **separate port**, so a healthcheck never becomes an HCI client.
Its JSON line carries the static configuration plus `pid`, `hci_clients`,
`hci_clients_total`, `hci_client_attached`, `peripheral_connections`,
`advertising` and `ready`. `ready` turns **false** (with a `fault` string) when
the peripheral has been connected with no HCI client attached, or neither
connected nor advertising, for more than 5 s (`STALE_FAULT_S`). In those states
no new central can find it. The bridge now prevents the first state; the check
is there in case it happens anyway.

`serve.py`'s own defaults bind **loopback** (`tcp-server:127.0.0.1:9000`, health
`127.0.0.1:9001`; review finding C12). Nothing here is authentication, and the
health line names the served file and its sha256. `docker-compose.yml` and
`Dockerfile.emulator` set `0.0.0.0` explicitly, which a container needs.

## 2. Running with Docker

```bash
docker compose build                                  # one-time; minutes (numpy/scipy/pyroomacoustics/av, meeteval compiled)
docker compose up -d --wait                           # the system: emulator + mockcloud healthy
docker compose run --rm job                           # the end-to-end check; exit code = verdict
docker compose logs emulator mockcloud
docker compose down -v                                # also drops the <project>_harness-build volume

./scripts/compose-smoke.sh                            # pin check → build (untimed) → time `up -d --wait` → run job → down -v
KEEP_UP=1 SKIP_BUILD=1 ./scripts/compose-smoke.sh     # reuse images, leave it running
COMPOSE_PROJECT_NAME=ci-a ./scripts/compose-smoke.sh  # isolated project: own network and volume
```

`compose-smoke.sh` first checks that the Bumble tree the images will copy is
at the pin. It compares `git rev-parse HEAD` of `reference/upstream/bumble` (or
`BUMBLE_TREE`) with `docs/reference-pins.txt`, checks there are no local
modifications (read-only: `GIT_OPTIONAL_LOCKS=0`, `--no-optional-locks`), and
checks that the three Dockerfiles carry the same `ARG BUMBLE_PIN`. A mismatch
exits **3**. A tree without `.git` gets a warning, because it cannot be
verified. The script then prints `up_wait_s=<n> target_s=60` and fails (exit
1) if the bring-up exceeds the target, `up --wait` fails, or the job exits
non-zero. It exits 2 when Docker, the compose v2 plugin or the daemon is
missing, and each exit-2 message points at `./scripts/local-up.sh`. The image
build is excluded from the timed window (HARNESS_POLICY: V6 is about bringing
the system live; the first pip install dominates by minutes and is printed
separately).

The job is behind the `check` profile so that plain `docker compose up -d --wait`
is well defined on every compose version: `--wait` treats a one-shot service
that exits 0 inconsistently across releases (`ServiceConditionRunningOrHealthy`
vs `service_completed_successfully`), and the job's `depends_on … service_healthy`
is honoured by `docker compose run` and `--profile check up` alike.

Reaching the services from the host (the default network is sealed, and Docker
cannot publish ports from an internal network):

```bash
docker compose -f docker-compose.yml -f docker/compose.host-ports.yml up -d --wait
python docker/probe.py scan tcp-client:127.0.0.1:9000      # a central checks the advertisement
curl http://127.0.0.1:8787/_mock/health ; curl http://127.0.0.1:9001/
```

Serving a generated meeting instead of the fixture (after the job has written
one into the volume):

```bash
docker compose run --rm job
PLAUD_MEETING_DIR=/app/build/compose/job/meetings/synth-smoke-s0001 docker compose up -d emulator
```

Compose forwards nothing from the shell by itself. The emulator's environment
carries `PLAUD_MEETING_DIR: ${PLAUD_MEETING_DIR:-}` and `PLAUD_FILE:
${PLAUD_FILE:-}`, so the value set on the command line reaches the container.
Because the resolved configuration changes, `up -d` recreates the emulator
(review finding C4: without the interpolation the command changed nothing and
the fixture kept being served). Run `docker compose up -d emulator` again
without the variable to go back to the fixture. `curl`ing the health line
(`file`, `file_source`) shows what is served.

### Bumble inside the images

The harness installs Bumble editable from the immutable reference tree, so the
images do the same from the **build-context copy** of `reference/upstream/bumble`
at the commit pinned in `docs/reference-pins.txt`
(`d371a27ebd8ab87b871a542df8442718b7882fdb`; `ARG BUMBLE_PIN` in every
Dockerfile). The copy carries no `.git` (excluded by the ignore files;
setuptools_scm would also need `git` in the image), so the version string the
venv reports — `0.1.dev1+gd371a27eb` — is handed to setuptools_scm via
`SETUPTOOLS_SCM_PRETEND_VERSION_FOR_BUMBLE`. That label is only true if the tree
on disk is at the pin. `compose-smoke.sh` enforces this before building, and
`tests/test_compose_topology.py` checks the local tree (review finding C14).
`scripts/fetch-references.sh` used to clone Bumble's upstream HEAD rather than
the pin (not this track's file), so on a fresh setup the check could fail.
(Update, 25 September 2026: the script now fetches every repository at its pin
from `docs/reference-pins.txt`.) On an older tree the remedy is still to check
out the pin in that tree. The alternative (clone the pin at
build time; needs git and network during the build only) is kept as a comment
in the Dockerfiles. Nothing under `reference/**` is modified by any of this.

### Compiler in the shared pip layer

`meeteval==0.4.3` ships only an sdist whose C++ extensions are mandatory
(`.github/workflows/tests.yml` header: "meeteval ships only an sdist"; the
venv's copy is a locally built wheel). `python:3.11-slim` has no compiler, so
the first version of the images could not have built (review finding C3). The
shared pip layer now runs `apt-get install --no-install-recommends
build-essential`, then the two `pip install`s, then `apt-get purge
--auto-remove build-essential` and removes the apt lists — all in one `RUN`. The
compiler never reaches a final layer. This covers any other pin that lacks a
wheel for the build platform (e.g. linux/arm64 on Apple Silicon). (Update,
25 September 2026: the first real build, on linux/arm64, installed
build-essential in this layer, built the images and completed; section 2.1.
No x86_64 build has run.)

The three Dockerfiles share one prologue byte for byte (base, pip layer,
package copies; a test checks), so BuildKit shares the layers. Only the last
lines differ. Each has a sibling `Dockerfile.<svc>.dockerignore` (BuildKit reads
it next to the Dockerfile) that keeps the 495 MB venv, `build/`, `data/`, the
Android rig and every reference tree except bumble out of the context.

### 2.1 Measured under Docker (2026-09-25)

Toolchain, installed without admin rights on this Mac: Colima, Lima and the Docker CLI
under `~/.local/opt/docker-tools` (linked from `~/.local/bin`), the Compose and Buildx
plugins under `~/.docker/cli-plugins`. Versions, the daemon and the image sizes are recorded
in `build/v6/docker-env-2026-09-28.txt`:
Colima 0.10.3, Lima 2.2.0, Docker CLI 29.8.1, Compose v5.5.1 and Buildx
v0.37.1. The Colima VM runs Ubuntu 24.04 with Docker daemon 29.5.2, 2 CPUs and
3 GB of memory. Lima, Colima and Compose were checked against their published
sha256 files. Buildx was checked against GitHub's asset digest. The Docker CLI
tarball has no published checksum; it came over HTTPS from download.docker.com.

The run's own lines (`build/v6/compose-smoke-2026-09-25.log`):

```
compose-smoke: bumble tree at the pin d371a27ebd8ab87b871a542df8442718b7882fdb
compose-smoke: build_s=121.38 (not part of the V6 window)
compose-smoke: up_wait_s=6.58 target_s=60 up_rc=0
JOB_OK elapsed_s=10.09 report=/app/build/compose/job/report.json cloud_report=/app/build/compose/job/cloud-report.json
compose-smoke: job_s=11.34 job_rc=0
compose-smoke: PASS system live in 6.58s (target 60s); job exit 0
```

* The `python:3.11-slim` base image was already on the machine (its `FROM`
  step took 0.1 s), so `build_s` excludes pulling it. It includes the
  `apt-get install build-essential` and both `pip install`s.
* Each image is 1.06 GB as `docker images` reported it; the three share their
  layers.
* The job ran every step in section 1: both probes, the scan (portVersion 7),
  2 × smoke generation, the oracle pipeline and `evals batch` (oracle suite
  PASS, 6 checks), and the cloud round trip (49 161 bytes in 3 parts,
  byte-exact download, task states `PENDING,STARTED,SUCCESS`, source
  `objectstore+meeting+pipeline-oracle`), then `evals score` (PASS). All
  scores are 0.0000, as a harness self-test must give.
* Still unverified: an x86_64 build and run, and a run on a GitHub runner.
  The `tests` workflow never calls `compose-smoke.sh`. On a runner, where a
  daemon answers, the suite resolves the file with `docker compose config`
  and skips the smoke test. Also unverified: other compose versions, and a
  cold build that also pulls the base image. The review's C11 (the running
  configuration's chunk size) is met by `--chunk-size 20000`: the Docker job's
  upload had 3 parts.

## 3. Running without Docker

```bash
./scripts/local-up.sh
EMULATOR_HCI_PORT=19000 EMULATOR_HEALTH_PORT=19001 MOCKCLOUD_PORT=18787 ./scripts/local-up.sh
```

The script:

1. **refuses ports that are taken.** If anything on 127.0.0.1 accepts
   connections on one of the three ports, or holds it bound, it prints
   `local-up: FAIL port N is already in use ...` and exits 1. A leftover
   local-up, another mockcloud or a published compose stack would otherwise
   answer the probes (review finding C6);
2. starts `emulator/serve.py` and `python -m mockcloud --chunk-size 20000` from
   the venv in the background;
3. waits on the same two predicates as the compose healthchecks
   (`docker/probe.py emulator|mockcloud`);
4. checks that both of its own processes are still alive and that the health
   line's `pid` is the emulator it started, then prints `LOCAL_UP_LIVE_S`;
5. runs `docker/job.sh` against `127.0.0.1` in the background and waits for it,
   then prints `LOCAL_UP_TOTAL_S` whether the job passed or not;
6. exits 1 with `local-up: FAIL job exited N` when the job fails (review
   finding C7: the job's own code used to leak out, and 2 collided with "venv
   missing"), or when a service died or the live time exceeds the target;
7. tears down on exit or signal. On `SIGINT`/`SIGTERM` the trap runs at once,
   because nothing long runs in the foreground. It freezes each process tree
   (`kill -STOP`), lists it again, then sends TERM and CONT, so the job's
   generator, pipeline or evals process dies with it (review finding C8).

Logs and the job output land under `build/compose/local/` (gitignored).

**Measured on this machine (Darwin 25.5.0, Apple Silicon arm64, CPython 3.11.16, 2026-09-25; three consecutive runs):**

```
LOCAL_UP_LIVE_S=1.22 / 1.23 / 1.22      both services healthy (includes the pid check)
CLOUD_OK ... elapsed_s=2.80 / 2.74 / 2.76   the cloud round trip alone (mock task step 1.0 s × 2 hops)
JOB_OK elapsed_s=9.50 / 9.04 / 9.08     probes + scan + 2 × smoke generation + oracle pipeline + evals
                                        (oracle suite PASS, 6 checks) + cloud round trip + evals score
                                        (oracle suite PASS on synth-smoke-s0001, 6 checks)
LOCAL_UP_TOTAL_S=10.80 / 10.33 / 10.38  including the job
local-up: PASS live in 1.22s (target 60s), total 10.80s including the job
```

The previous figures (2026-09-24: live 0.94 s, total 6.60 s) predate the cloud
step and the pid check. A container adds image start-up and healthcheck cadence
(2 s interval) on top of this. Before the first Docker run this section called
the 60 s claim "correct by construction", not "measured". Under Docker on
2026-09-25 the services were healthy in 6.58 s (section 2.1).

The scan step's output is the emulator's actual advertisement decoded by
`plaudsim.advertising.parse_manufacturer_data`:
`address F0:1A:2B:3C:4D:5E, company 0xFFFF, port_version 7, serial 8810000001, V0001`.

### The cloud round trip (job steps 7–8)

`docker/cloud_roundtrip.py` takes the first generated meeting, reads its
primary device file from `meeting.json` (`audio.device_primary` →
`device/recording.ogg`, sha256 checked against `audio.device_details`) and
drives the mock over plain HTTP (stdlib `urllib`, only the `--url` given):

| step | request | checked |
|---|---|---|
| register | `POST /_mock/meetings {"dir"}` | the device file is among the fingerprinted files (the oracle-path knob, docs/mockcloud.md) |
| identity | Basic → partner token → `users/access-token` | a bearer user token (never printed) |
| bind | `POST sdk/bind {notepro, 8810000000000001}` | `is_bind: true` |
| presign | `generate-presigned-urls` | **≥ 2 parts** (49 161 bytes at ChunkSize 20 000 = 3); with the documented 5 MiB it is one part and the step fails, naming `--chunk-size` |
| upload | `PUT` each part | ETag = quoted MD5 of the part |
| complete | `complete-upload` | `FileMd5` = MD5 of the file |
| download | `GET DownloadUrl` | byte-exact |
| transcribe | `POST ai/transcriptions/`, poll | `PENDING,STARTED,SUCCESS`; task source `objectstore+meeting+pipeline-oracle` (read from `/_mock/state`) |
| hypothesis | `mockcloud.oracle.result_to_hypothesis` → `evals.io.write_hypothesis_files` | `hyp/cloud/<meeting_id>/hyp.json` (+ `.rttm`, `.stm`) |
| unbind | `POST sdk/unbind` (also on failure) | the mock's binding is left as it was |

`evals score --suite oracle` then scores that `hyp.json` against the meeting:
cpWER, tcpWER, DER and JER are 0.0000, the speaker-count error is 0, and the
gate passes (`cloud-report.json`, `cloud-report.md`). The label-literal WER is
2.0, because the cloud says `Speaker 1` where the reference says `spk0` (the
cpWER rationale, docs/integration.md). **This is a harness self-test.** The
mock answers a recognised upload with the meeting's ground truth (through
`pipeline.oracle.OraclePipeline`). The zero shows the upload, task and
conversion wiring is right. It says nothing about any ASR. The partner-app
credentials are the mock's SYNTHETIC ones from `mockcloud/settings.py`.

## 4. Probes and attaching your own central

`docker/probe.py` is the healthchecks' twin plus one thing a one-liner cannot
do. Each subcommand retries until success or `--timeout` and prints one
`PROBE_OK`/`PROBE_FAIL` line:

```
python docker/probe.py tcp        HOST:PORT
python docker/probe.py emulator   HOST:PORT        # health line ready:true (prints it)
python docker/probe.py mockcloud  URL              # GET URL/_mock/health
python docker/probe.py scan       tcp-client:HOST:9000 [--address F0:1A:2B:3C:4D:5E] [--company-id 0xFFFF] [--port-version 7]
```

**The whole run is bounded by `--timeout`** (review finding C2), plus a second
or two of teardown. Each scan attempt — opening the transport, the host's
`HCI_Reset`, starting the scan, waiting for the advertisement — runs under one
deadline of `min(remaining, 10 s)`. Bumble's `Host.reset` waits for its Command
Complete with no timeout of its own (`bumble/host.py`, `send_sync_command` with
`response_timeout=None`), so a port that accepts TCP but never speaks HCI used
to hang the probe, and the job with it. The scan also **checks what it
reports** (review finding C5). An advertisement from the expected address must
carry manufacturer data under `--company-id`, and that data must decode, via
`plaudsim.advertising`, to `--port-version`. Otherwise the probe fails at once
with the reason and what it saw, because retrying cannot change the verdict.
The job passes `--company-id 0xFFFF --port-version 7` explicitly.

Your own Bumble host attaches the same way the probe does:

```python
async with await open_transport("tcp-client:127.0.0.1:9000") as hci:
    device = Device.with_hci("central", Address("F0:F1:F2:F3:F4:F6"), hci.source, hci.sink)
    await device.power_on()
    connection = await device.connect("F0:1A:2B:3C:4D:5E")     # then use plaudsim's client side
    ...
    await connection.disconnect()                               # polite; walking away is now handled too
```

Each client gets its own controller, so two centrals can attach at once. The
peripheral still takes one connection at a time: its advertiser stops while it
is connected, as a real device's would (`controller.py:740`). A second central
can therefore only find it after the first disconnects or goes away.

## 5. HARNESS_POLICY register (choices, not evidence)

1. Bridge mode: the peripheral on an in-process virtual controller; **one fresh virtual controller per TCP client** of `PLAUD_HCI_TRANSPORT`, released on disconnect (`LL_TERMINATE_IND` reason 0x13 for each LE link it held, scan/connect/advertising state cleared, removed from the link); `direct` mode kept for the R7 rig; a non-`tcp-server` bridge transport keeps one shared controller.
2. Defaults: `tcp-server:127.0.0.1:9000`, health `127.0.0.1:9001` (loopback; containers set `0.0.0.0` explicitly), device `PlaudNotePro` / `F0:1A:2B:3C:4D:5E`, bridge controllers `F0:F1:F2:F3:F4:F5`, probe central `F0:F1:F2:F3:F4:F6`, manufacturer company id `0xFFFF`, session id `1700000000`, file-table entry `scene 2 / attribute 1` (all from `r7/pull_capture_peripheral.py`).
3. Served file precedence `PLAUD_FILE` > `PLAUD_MEETING_DIR/device/recording.ogg` > R6-S2 fixture; set-but-missing is exit 2; empty = unset; compose interpolates both from the shell.
4. `stream_in_task=True, response_pacing_s=0.004` (R7-S13).
5. Health on a separate port; `ready` false after a fault persists 5 s (`STALE_FAULT_S`, sampled every 0.5 s); health `interval 2s / timeout 3s / retries 30 / start_period 5s`; no `start_interval` (needs Engine 25+).
6. Job: `generator batch --n 2 --scenario smoke --seed 1`, `pipeline batch --pipeline oracle --fail-fast`, `evals batch --suite oracle`; cloud round trip of the first meeting as user `harness-job-0001` on SN `8810000000000001` (the mock's seeded notepro), `JOB_CLOUD_TIMEOUT` 60 s, 10 s per HTTP request, then `evals score --suite oracle`; output under `/app/build/compose/job`; probe timeout 30 s, scan attempt ≤ 10 s; the oracle and the mock's ground-truth answer are harness self-tests, not systems under test.
7. Job behind profile `check`; `restart: "no"`; `--wait-timeout 2 × target` in the smoke script; build excluded from the timed window; Bumble pin checked before anything Docker-related (exit 3).
8. Network `harness`, bridge, `internal: true`, nothing published, no explicit `name:` (project-scoped); `docker/compose.host-ports.yml` publishes on `127.0.0.1` only. No secrets, no `env_file`, no external network, no image beyond `python:3.11-slim`.
9. Named volume `harness-build` (project-scoped) at `/app/build`; mockcloud `--local-file-root /app/build --chunk-size 20000` (documented ChunkSize 5 MiB) so the job's upload is a genuine multipart one.
10. Images: `python:3.11-slim`; `build-essential` installed and purged inside the pip layer; `pip install -e` of the context copy of bumble with a pretend setuptools_scm version; `requirements/all.txt`; `PYTHONPATH=/app:/app/emulator`; one byte-identical prologue; per-Dockerfile ignore files kept identical.
11. Container mockcloud binds `0.0.0.0` (the module default `127.0.0.1` is unreachable from other containers).
12. local-up: refuse taken ports (connect or `SO_REUSEADDR` bind on 127.0.0.1); verify its own PIDs (`kill -0`) and the emulator's health `pid`; job in the background with `wait`; tree teardown by STOP → re-list → TERM + CONT.

## 6. Tests

`tests/test_compose_topology.py` (40): compose YAML parses; the three services;
job depends on both with `service_healthy`, profile `check`, `restart: no`;
named volume mounted by all; **network and volume carry no explicit `name:`**
(C9); **resolved with `docker compose config` when the CLI is present: names
follow `-p`, a shell `PLAUD_MEETING_DIR` reaches the emulator, unset stays
empty** (C4, C9); network internal, nothing published, no `network_mode` /
`env_file` / `secrets`; every build context, Dockerfile, `COPY` source and
`CMD` path exists; **the three prologues are byte-identical and the pip layer
installs build-essential before pip and purges it after** (C3); **every
`$HERE/*.py|sh` helper job.sh runs exists and is COPY'd**; mockcloud binds
`0.0.0.0`; Dockerfiles carry the bumble pin; **the local bumble tree is at the
pin** (`git rev-parse`, or the installed version's `+g<sha>` without `.git`)
(C14); the three ignore files are identical and exclude the right trees;
host-ports override binds loopback only; the emulator healthcheck targets
`serve.DEFAULT_HEALTH_PORT`, and **the compose env and the image bind `0.0.0.0`
explicitly while `serve.py` defaults to loopback** (C12); **the served-file
knobs are interpolated and empty means the fixture** (C4); **the mockcloud
chunk size makes a smoke upload multipart and matches local-up and the image**;
the mockcloud healthcheck's path is a route `create_app()` really registers;
healthcheck one-liners are valid Python; job env points at the service names,
an existing gate suite, an existing generator preset and a cloud timeout;
scripts are executable and pass `bash -n`; **`compose-smoke.sh` exits 2 with a
pointer to local-up when the CLI is missing, when the compose plugin is
missing, and when the daemon is unreachable (fake `docker` shims on PATH), and
exits 3 for a Bumble tree off the pin even when "Docker" works** (C13, C14);
the real-PATH run exits 2 unless a daemon answers (then it skips as
`docker is present`); probe help lists the probes and the new flags;
`serve.py` recording resolution order and defaults; **health turns `ready:
false` after 5 s of a stuck peripheral, naming the fault** (C1); bridge
transport spec parsing; this document says whether V6 ran under Docker.

`tests/test_compose_runtime.py` (18): `serve.py` opens the health and HCI ports
within 20 s, its status carries the policy values and the live fields, the
health port answers HTTP too, a central attached via `tcp-client` receives the
advertisement decoded to portVersion 7; exit 2 on a missing meeting dir;
serving a generated meeting's `device/recording.ogg`; **the bridge still
serves a scan after a central connected and walked away, after one left while
scanning, and after one died two bytes into an HCI packet, and serves two
centrals at once** (C1); **`probe.py scan --timeout 3` against a listener that
never answers exits 1 within 8 s** (C2); **the scan fails for a wrong
`--port-version` / `--company-id` and for an imposter advertising only a name
at the right address** (C5); `python -m mockcloud` answers `/_mock/health`
within 20 s; **`cloud_roundtrip.py` pushes a generated recording through a
spawned mock in 3 parts and the result passes `evals score --suite oracle`,
and it refuses a one-part upload**; the process-group helper leaves no orphan
(C8); `scripts/local-up.sh` completes within 60 s with `JOB_OK`, `CLOUD_OK`
and two passing reports; **refuses a squatted port** (C6); **reports a failing
job as `FAIL` / exit 1 with `LOCAL_UP_TOTAL_S`** (C7); **honours SIGTERM
during the job in under 5 s with nothing left in its process group** (C8). The
local-up tests run it in its own session and kill the whole group on timeout.
Ephemeral ports throughout.

The bold items were run against the pre-fix code on 2026-09-25, and all of
them failed except three guards that already held: the local tree is at the pin,
`Dockerfile.emulator` already set `0.0.0.0`, and the missing-CLI branch already
exited 2. The symptoms: walk-away `PROBE_FAIL ... TimeoutError()`; mid-scan
`COMMAND_DISALLOWED_ERROR`; mid-packet and silent listener hung past the probe's
timeout (killed by the test's 40 s / 25 s limits); imposter `PROBE_OK`; wrong
`--port-version` rejected by argparse (rc 2); squatted port `local-up: PASS`;
failing job rc 2 with no FAIL line; SIGTERM took 8.3 s; with an off-pin tree and
a "docker" that always succeeds, `compose-smoke.sh` ran to `PASS`; plugin and
daemon messages had no local-up pointer; the resolved config showed
`plaud-harness` for `-p other-project` and no `PLAUD_MEETING_DIR`. The orphan
mechanism behind C8 was shown separately: `subprocess.run(["bash", "-c",
"sleep … & sleep …"], timeout=1)` left both sleeps running.

```
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_compose_topology.py tests/test_compose_runtime.py -q -p no:cacheprovider --timeout=120
```

## 7. Review findings (2026-09-25) and what changed

| id | finding | change | proving test |
|---|---|---|---|
| C1 | shared bridge controller: a central leaving badly broke the service; health still ready | per-client controllers released on disconnect; live health fields and `ready:false` on a stuck peripheral | `test_bridge_recovers_*`, `test_bridge_serves_two_centrals_at_once`, `test_health_turns_not_ready_when_the_peripheral_is_stuck` |
| C2 | `probe.py scan --timeout` did not bound the attempt | whole attempt under one deadline; total ≤ `--timeout` | `test_probe_scan_is_bounded_by_its_timeout_against_a_silent_listener` |
| C3 | no compiler for meeteval's sdist | build-essential installed and purged in the pip layer | `test_dockerfile_prologues_are_identical_and_compile_sdists` (static; no build run) |
| C4 | `PLAUD_MEETING_DIR=... docker compose up` had no effect | `${PLAUD_MEETING_DIR:-}` / `${PLAUD_FILE:-}` interpolation | `test_emulator_environment_forwards_the_served_file_knobs`, `test_resolved_config_scopes_names_and_forwards_the_served_file_knobs` |
| C5 | scan passed any advertiser at the address | `--company-id` / `--port-version` verdict | `test_probe_scan_rejects_*` |
| C6 | local-up passed against a foreign listener | port refusal, `kill -0`, health `pid` check | `test_local_up_refuses_a_port_that_is_already_taken` |
| C7 | failing job: no FAIL line, exit code collided with "venv missing" | job awaited with `set +e`; FAIL + exit 1; total always printed | `test_local_up_reports_a_failing_job_as_fail_with_exit_1` |
| C8 | killed local-up left orphans; SIGTERM deferred | background job + `wait`; tree teardown; tests kill the process group | `test_local_up_tears_its_whole_tree_down_promptly_on_sigterm`, `test_run_group_kills_the_whole_tree_on_timeout` |
| C9 | global network / volume names | names dropped (project-scoped) | `test_network_and_volume_names_are_project_scoped`, resolved-config test |
| C10 | integration test pinned the oracle-hook gap | assertions now pin `+pipeline-oracle` (the gap is closed in mockcloud/oracle.py) | `tests/test_integration_mockcloud_roundtrip.py` |
| C12 | bare `serve.py` listened on every interface | loopback defaults; containers explicit | `test_serve_config_defaults_and_r7_s13_policy`, `test_emulator_dockerfile_binds_every_interface_explicitly` |
| C13 | docker-absent test skipped for the wrong reason; doc-status test blocked a real V6 record | shim-driven tests for all three exit-2 branches; doc test accepts `NOT executed` or a measured `up_wait_s=` | `test_compose_smoke_exits_2_*`, `test_docs_state_whether_v6_was_executed_with_docker` |
| C14 | Bumble pin never enforced | pin check in compose-smoke (exit 3) and in the tests | `test_compose_smoke_refuses_a_bumble_tree_off_the_pin`, `test_reference_bumble_tree_is_at_the_pinned_commit` |

## 8. Not done, and why

* **V6 beyond one machine.** Correction, 25 September 2026: this item used to
  say that `docker compose build` and `up -d --wait` had never run. They ran
  once on 2026-09-25 (section 2.1). Still unverified: the build and the timing
  on x86_64, on a GitHub runner, with other compose versions, and from a cold
  cache that also pulls the base image.
* **Recovering from a stuck emulator in compose.** If `ready` ever turns false,
  the healthcheck marks the container unhealthy, but Docker's
  `restart: unless-stopped` restarts a container only when it exits, never
  when it is unhealthy. `docker compose restart emulator` is the remedy. The
  stuck states the review found are now prevented rather than merely reported.
* **Real central in the stack.** No container runs the Android template or
  netsim; the "central" in the job is the Bumble probe. `PLAUD_HCI_MODE=direct`
  plus the R7 rig is how a real SDK is attached. The job does not pull the
  recording over BLE (tests/test_integration_emulator_serves_generator.py does,
  in-process).
* **The cloud step's zero is not an ASR score.** The mock answers with ground
  truth by design. A real ASR behind the mock, or the real cloud, is outside
  this track.
* **TLS** in front of mockcloud for the template app (docs/mockcloud.md caveat)
  is not part of the topology.
* **Per-step watchdog in job.sh.** Each step is bounded by its own tool (probe
  deadline, cloud timeout, per-request limit). There is no outer `timeout`
  wrapper; macOS has no `timeout` binary by default.
