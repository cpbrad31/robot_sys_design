# Robot Fleet Monitor (simulation)

This was an educational project to understand system design tradeoffs for an application to monitor and control a fleet of robot arms deployed in the field. Key learnings and concepts:

Telemetry: A hybrid strategy for accesssing robot telemetry data helps us get important data frequently (health status) while shifting higher bandwidth transfers to less frequent, offline syncs (camera and motion 
torque data)

Data and control plane: We can separate the flow of data between the robot and a server into data and control planes - data plane is jsut sending information about the robot vs. the control plane is driving action (i.e. pause, firmware update)

Robot system design: We can think about a robot arms control in terms of the control loop (air gapped from outside connections for safety) and the cloud agent (responsible for communicaiton to the outside server)

Firmware updates: To reduce risk of updates causing issues you have to triage manually with a technician, it's best to keep multiple firmware versions on device so that a failed boot can rollback. If an updated version fails to pass a predefined set of health checks (actions you don't want to lose, like server comms and home pose) then rollabck to the last one. Fleetwide deployments also happen gradully - deploy to a test set of canaries before ramping across the rest of the fleet.



## Setup (once)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python -m fleet.server                 # terminal 1 -> http://127.0.0.1:8000 (ws on :8001)
.venv/bin/python -m fleet.simulate --robots 8    # terminal 2
```

Click a robot card on the dashboard to see the telemetry log the server received from it. Entries delivered late (buffered during an outage) are highlighted.

Server options: `--port 8000`, `--ws-port 8001`, `--offline-after 6` (seconds before a robot shows offline / a log entry counts as late).

Fleet options: `--interval 2.0` (heartbeat seconds), `--outage-every 30` (seconds of normal operation between outages, `0` disables outages), `--outage-duration 20` (seconds the downed robot stays offline).

Quick demo of an outage: `--outage-every 10 --outage-duration 15`.

`--seed N` makes a run reproducible (the seed in use is logged at startup; by default it's random each run).

## Firmware updates

The server offers firmware `2.0.0`; robots start on `1.0.0`/`1.1.0`/`1.2.0`. Click **Update to v2.0.0** on a card to update that one arm. The robot runs an A/B update, reporting each step as a status:

1. `DOWNLOADING`: fetch the artifact from the server, verify its SHA-256, write it to the inactive slot
2. `REBOOTING`: restart into the new slot
3. `TRIAL_BOOT`: run the new firmware on probation (the running version now shows 2.0.0)
4. 50/50: **commit** (new firmware stays, result `committed`) or `ROLLING_BACK` (reboot into the old slot, result `rolled_back`)

A failed or corrupt download leaves the slots untouched (result `download_failed`). Each card shows both slots (the running one in bold) and the last update result; a rolled-back robot can be updated again. The arm is out of service during an update, so Pause is disabled.

Each phase lasts a few seconds (6s / 4s / 8s / 4s), so an update takes roughly 20 seconds.

## Stop / restart

```bash
pkill -f "fleet\.(server|simulate)"     # kill server + fleet, frees ports 8000/8001
```

If a server fails with "Address already in use", an old process is still running: run the `pkill` above first.

## Reset data

```bash
rm -f data/heartbeats.jsonl             # clear the heartbeat log (in-memory state resets on server restart)
```

## Test

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

## API

- `GET /` dashboard
- `GET /api/robots` latest status per robot (includes `connected`: websocket open)
- `GET /api/robots/<id>/heartbeats?limit=50` received log (each entry has `delay_s` and `late`)
- `POST /api/heartbeats` body `{"heartbeats": [...]}`: ingest a batch (idempotent); replies `{accepted, duplicates, rejected}`
- `POST /api/heartbeat` ingest a single heartbeat
- `POST /api/robots/<id>/command` body `{"command": "pause" | "resume"}` (409 if robot not connected)
- `POST /api/robots/<id>/firmware` body `{"version": "2.0.0"}` start an update (400 unknown version, 409 if not connected / already updating / already on that version)
- `GET /api/firmware` available versions and the latest; `GET /api/firmware/<version>` downloads the artifact
- `ws://127.0.0.1:8001` robots connect, send `{"type":"hello","robot_id":...}`, then receive commands
