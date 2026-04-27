# Avalon Q Controller

Self-hosted controller and scheduler for one or more **Canaan Avalon Q** home Bitcoin miners. Runs on [Umbrel](https://umbrel.com/). No cloud, no telemetry, no account — your credentials, schedules, and 24-hour metrics history live only on your home server.

![Dashboard](https://raw.githubusercontent.com/gbechtel-beck/umbrelsolostrike-app-store/main/umbrelsolostrike-avalon-q-controller/1.jpg)

> **Already run Home Assistant?** Check out [c7ph3r10/ha_avalonq](https://github.com/c7ph3r10/ha_avalonq) — a great HA template for the Avalon Q. This project is for everyone else: Umbrel users who want one-click install, multi-miner support, scheduled pool rotation, and proper 24-hour history charts without setting up Home Assistant.

---

## Why this exists

The Avalon Q's built-in web UI is deliberately minimal — it lets you set the pool config and trigger a reboot, and that's it. To change the workmode (Eco / Standard / Super), put the miner in standby, or wake it back up, you have to use the official Canaan phone app. That's fine for one-off changes, but it falls apart fast if you want to:

- **Drop to standby during expensive electricity hours** (e.g. APS on-peak 4–7pm) and bring it back automatically
- **Rotate between pools** on a schedule (try a solo pool overnight, fall back to a shared pool by day)
- **Change workmode without picking up your phone** every time
- **See a 24-hour history** of hashrate, power draw, and temperature instead of just the live numbers
- **Manage more than one Avalon Q** without juggling phone-app sessions
- **Audit every command** sent to the miner, with timestamps

…none of that is in the firmware or the phone app. This controller fills the gap. Everything the phone app does (workmode, soft-off, soft-on, LCD, reboot) is exposed in the manual control panel, and on top of that you get scheduling, history, multi-miner support, and a real audit log.

It works equally well with a single miner — you don't need a fleet to benefit from time-of-use scheduling and history charts.

---

## How it works

The app talks directly to each miner's **CGMiner-compatible API on TCP port 4028** over your local network. It polls every miner every 30 seconds, persists samples to SQLite, and applies any schedule actions immediately. All control flows through `ascset` commands — the same ones the official Avalon firmware exposes — so nothing about the miner's behavior is unsupported or fragile.

Multi-miner support is built in from the ground up. Add miners by IP, or use the LAN scanner to auto-discover them. Each miner has its own pool library, schedule, history charts, and event log.

---

## Install

Add this community app store to your Umbrel:

1. Open your Umbrel dashboard
2. App Store → three-dot menu → **Community App Stores** → **Add**
3. Paste the URL: `https://github.com/gbechtel-beck/umbrelsolostrike-app-store`
4. Find **Avalon Q Controller** in the new store and click **Install**

That's it. The app will be at `http://your-umbrel:8118` once it boots.

If you're not on Umbrel, see the [Standalone Docker](#standalone-docker) section below.

---

## What you can do

### Dashboard
Live per-miner status: workmode, hashrate, real wall-draw power, J/TH efficiency, peak chip temperature, chassis temp, fan duty, uptime, accepted/rejected shares, and the current active pool.

![Dashboard](https://raw.githubusercontent.com/gbechtel-beck/umbrelsolostrike-app-store/main/umbrelsolostrike-avalon-q-controller/1.jpg)

### Charts
24-hour history per miner. Hashrate, power draw, hashboard max temperature, and workmode are sampled every poll cycle and stored in SQLite. Soft-off windows and workmode changes are visible in the time series.

![Charts](https://raw.githubusercontent.com/gbechtel-beck/umbrelsolostrike-app-store/main/umbrelsolostrike-avalon-q-controller/2.jpg)

### Pool library
Define your stratum pools per miner (URL, worker, password, optional notes). One-click apply with an optional reboot to make the change take effect immediately. The Avalon Q firmware otherwise applies pool changes on next reboot — the app is explicit about this so you can choose.

### Schedule
Build rules that map a `(days × time-window × season) → action` tuple. Available actions:

- **Workmode change** (Eco / Standard / Super)
- **Soft-off** (standby) and **Soft-on** (wake)
- **Pool switch**, optionally chained with a workmode change and an optional reboot

Time windows can wrap past midnight, seasons limit rules to a date range (e.g. winter rates only), and rule priority breaks ties when two windows overlap. A **default action** runs whenever no rule matches, so the miner is never left in an undefined state.

![Schedule](https://raw.githubusercontent.com/gbechtel-beck/umbrelsolostrike-app-store/main/umbrelsolostrike-avalon-q-controller/3.jpg)

### Manual control
Direct buttons for workmode, soft-off/on, LCD on/off, and reboot. A pause toggle and a timed override let you take ad-hoc control without losing your rule definitions.

### Events
Reverse-chronological audit log of every command issued, every state transition observed, and every config change. Available in the UI and via the API.

---

## Example: APS time-of-use schedule

For Arizona Public Service customers on the time-of-use rate plan, on-peak runs Mon–Fri 4–7pm at $0.099/kWh — about 3× the off-peak rate. A simple schedule that drops to standby during that window:

| Field | Value |
|---|---|
| Timezone | `America/Phoenix` |
| Default action | `super — workmode 2 (~1600 W)` |
| Rule name | `APS on-peak` |
| Action | `off — soft-off (standby)` |
| Days | Mon, Tue, Wed, Thu, Fri |
| Start | `16:00` |
| End | `19:00` |
| Priority | `0` |

The default action handles all hours outside that window, so the miner runs Super 21 hours/day and stays off for 3. When the on-peak window ends at 7pm, the default action kicks in and the controller automatically wakes the miner from soft-off and sets workmode to Super.

---

## Compatibility

Tested against:

- **Avalon Q (Q_MM1v1_X1, FW 25052801_14a19a2)** — confirmed working
- Should work with any Avalon Q firmware exposing the standard CGMiner JSON-RPC API on port 4028 with `ascset` writes for `workmode,set,N`, `softoff`, `softon`, `lcd`, `reboot`, and `setpool`

If you have a different Avalon model and want to try it: add a miner by IP, see what the dashboard reads. If anything is misparsed, open an issue with the output of:

```bash
echo '{"command":"stats"}' | nc <miner-ip> 4028
```

I'll add support if the format is reasonable.

---

## Standalone Docker

If you don't run Umbrel, the same image works with plain Docker:

```bash
docker run -d \
  --name avalon-q-controller \
  --restart unless-stopped \
  -p 8118:8000 \
  -v avalon-q-data:/data \
  ghcr.io/gbechtel-beck/avalon-q-controller:latest
```

Then open `http://localhost:8118`.

The image is published to GHCR for `linux/amd64` and `linux/arm64`.

---

## Architecture

- **FastAPI** for the HTTP API and HTML
- **Vanilla JS** front-end (no build step, no React, no bundler) — easy to fork and modify
- **SQLite with WAL** for persistence; 24h sample retention, 30d event retention
- **Async poll loop per miner** so a slow or offline miner never blocks the others
- **Single Docker image**, no external dependencies (no Postgres, no Redis)

The CGMiner protocol is plaintext over TCP. Wire format for the Avalon Q's `ascset` writes (worth knowing if you fork this for another miner):

```
ascset|0,workmode,set,0       # Eco
ascset|0,workmode,set,1       # Standard
ascset|0,workmode,set,2       # Super
ascset|0,softoff,1:<ts+5>     # Standby (5-second delayed trigger)
ascset|0,softon,1:<ts+5>      # Wake from standby
ascset|0,lcd,0:1              # LCD on
ascset|0,lcd,0:0              # LCD off
ascset|0,reboot,0
ascset|0,setpool,<url>,<worker>,<password>
```

---

## Roadmap

- Multi-arch image (arm64 build for Raspberry Pi Umbrels)
- CSV export of history for offline analysis
- Webhook notifications on schedule events and miner offline alerts
- Optional integration with Home Assistant for cross-system automation

Open issues for specific requests.

---

## License

MIT. Use it, fork it, modify it, ship it.

## Acknowledgements

- The wire format for the `ascset` writes was figured out with help from [c7ph3r10/ha_avalonq](https://github.com/c7ph3r10/ha_avalonq), the Home Assistant template for the Avalon Q. If you use Home Assistant, that project is a great companion.
- Built and tested on a real Avalon Q running at home in Arizona.
