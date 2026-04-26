# Avalon Q Controller

A self-hosted controller, scheduler, and dashboard for one or more
Canaan **Avalon Q** home Bitcoin miners.

Talks directly to each miner's CGMiner-compatible API on TCP port 4028.
No cloud. No telemetry. Your credentials, schedules, and metrics history
live only on your Umbrel.

---

## What it does

- **Fleet dashboard.** Manage one or many Avalon Qs from a single sidebar.
  Live status, hashrate, power, temps, fan, shares, and active pool per
  miner.
- **24-hour history charts.** Hashrate / power / temperature / workmode
  sampled every poll cycle and rendered as inline canvas charts. Stored
  in SQLite, automatically pruned to the last 24h.
- **Pool library.** Define a set of stratum pools per miner (URL, worker,
  password). Apply any pool with one click, with or without auto-reboot.
- **Pool-rotation scheduler.** Rules can switch the active pool, change
  workmode, soft-off, or soft-on. Each rule is
  (days × time-window × season) → action with priority ordering.
  Time windows can wrap past midnight; seasonal windows can wrap past
  year boundaries.
- **Per-rule pool-switch options.** A rule that switches pools can
  optionally:
  - chain a workmode change (e.g. "switch to BTC pool **and** drop to Eco"),
  - reboot the miner so the new pool takes effect immediately.
- **Manual override + pause.** Force any action indefinitely or for N
  minutes; pause the scheduler entirely without losing rules.
- **Manual control panel.** Workmode, soft-off, soft-on, LCD on/off,
  reboot.
- **LAN discovery.** Scan your local /24 (or any CIDR up to /22) for
  CGMiner-API devices and add them with one click.
- **Full audit log.** Every command, applied action, schedule edit,
  override, and pool change is recorded in SQLite for 30 days.

---

## Pool-switching honest note

The Avalon Q firmware applies pool changes on **next reboot**. The
controller can send the reboot for you (per-rule option), but every
reboot forfeits any in-flight shares — keep that in mind when planning
rotations during high-difficulty work or near a found block submission.

If you want zero-downtime pool switching, point all miners at a local
stratum proxy (your AxeDGB, your CK Pool, or a tiny custom proxy) and
switch what *that* proxy forwards to. v3 may add proxy support
natively.

---

## Defaults & assumptions

- Avalon Q web admin password defaults to `admin` from the factory.
  This app uses that as the default for new miners — change it on the
  miner's web UI if you've not already done so.
- Poll interval defaults to 30 seconds per miner.
- Sample retention: 24 hours.
- Event log retention: 30 days.
- Discovery scan refuses to enumerate networks larger than /22 (1024
  addresses).

---

## Run on Umbrel

The app is structured as an Umbrel community-store app. Place this
repo's `umbrel-community/` directory under your Umbrel community apps
source, set `repo:` to your fork, and install through the Umbrel UI.

---

## Run standalone (Docker)

```bash
docker compose up -d --build
# open http://<host>:8118
```

Data persists in a Docker volume named `avalonq_data`.

---

## API surface

The app exposes a REST API at `/api/...`:

- `GET /api/miners` · `POST /api/miners` · per-id GET/PATCH/DELETE
- `POST /api/miners/{id}/refresh` — force an immediate poll
- `GET /api/miners/{id}/status` — most-recent status snapshot
- `PUT /api/miners/{id}/schedule` — replace schedule
- `POST /api/miners/{id}/pause` `{"paused": true|false}`
- `POST /api/miners/{id}/override` `{"action":"...","duration_minutes":N}`
- `DELETE /api/miners/{id}/override`
- `POST /api/miners/{id}/command/{eco|standard|super|soft_on|soft_off|reboot|lcd_on|lcd_off}`
- `GET/POST /api/miners/{id}/pools` · `PATCH/DELETE /api/pools/{id}` · `POST /api/pools/{id}/apply`
- `GET /api/miners/{id}/history` — last 24h samples
- `GET /api/miners/{id}/events` — per-miner events · `GET /api/events` — all
- `POST /api/discovery/scan` `{"cidr": "192.168.1.0/24"}`
- `GET /api/discovery/subnet` — auto-detected local /24
- `GET /api/timezones` — IANA tz list

OpenAPI/Swagger docs at `/docs`.

---

## Schedule actions

| action          | meaning                                             |
| --------------- | --------------------------------------------------- |
| `none`          | leave miner alone                                   |
| `off`           | soft-off (standby — workmode 3)                     |
| `on`            | soft-on (resume from standby)                       |
| `eco`           | workmode 0 (~800 W)                                 |
| `standard`      | workmode 1 (~1300 W)                                |
| `super`         | workmode 2 (~1600 W)                                |
| `pool:<id>`     | switch primary pool to the given pool from the library |

Pool actions also honor each rule's `chain_workmode` (eco/standard/super)
and `reboot_after_pool_switch` flags.

---

## Example: Gil's APS rate-aware day (America/Phoenix)

```json
{
  "timezone": "America/Phoenix",
  "default_action": "pool:1",
  "rules": [
    {"name":"KAS in cool morning","action":"pool:2",
     "days":[0,1,2,3,4],"start":"06:00","end":"10:00",
     "priority":10,"chain_workmode":"super","reboot_after_pool_switch":true},

    {"name":"DGB super during solar","action":"pool:1",
     "days":[0,1,2,3,4],"start":"10:00","end":"15:00",
     "priority":15,"chain_workmode":"super"},

    {"name":"BTC mid-afternoon","action":"pool:3",
     "days":[0,1,2,3,4],"start":"15:00","end":"16:00",
     "priority":20,"chain_workmode":"standard","reboot_after_pool_switch":true},

    {"name":"APS on-peak soft-off","action":"off",
     "days":[0,1,2,3,4],"start":"16:00","end":"19:00",
     "priority":100},

    {"name":"DGB evening eco","action":"pool:1",
     "days":[0,1,2,3,4],"start":"19:00","end":"22:00",
     "priority":10,"chain_workmode":"eco"}
  ]
}
```

---

## License

MIT. See `LICENSE`.
