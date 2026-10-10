# Jasim Slack Monitor

A lightweight Python-based network monitor node. Pings devices on a schedule, detects state changes, and sends formatted alerts directly to a Slack channel via the Slack Bot API.

## Architecture

```
Monitor Node (Python)
  ├── Two-tier scheduler (critical / standard threads)
  ├── ICMP ping per device
  ├── State-change detection (in-memory)
  ├── Escalation tracking (repeating alerts while down)
  └── Slack API (chat.postMessage)
          │
          ▼
    Slack Channel
```

## File Structure

```
Jasim_Slack/
├── run.py                  # Entry point, loads .env, configures logging
├── start.bat               # Windows launcher (creates venv, installs deps, runs)
├── requirements.txt
├── .env.example            # Credentials template
├── .env                    # Local credentials
├── config/
│   └── devices.json        # Device list and thresholds
├── src/
│   ├── monitor.py          # Scheduler, ping, state tracking, escalation
│   ├── sender.py           # Slack API sender + message formatting
    └── detector.py         # Infra failure detection
```

## How It Works

### Scheduling

Two independent threads run concurrently:
- **CRITICAL** — pings every 60s (±10% jitter)
- **STANDARD** — pings every 300s (±10% jitter)

Devices are assigned to threads based on `importance` in `devices.json`. Anything not marked `critical` goes to the STANDARD thread.

Pinging is cross-platform: `ping -c 1 -W <timeout>` on Linux, `ping -n 1 -w <timeout_ms>` on Windows. Latency is parsed from either output format.

### State Tracking

State is held in-memory per scheduler thread. A Slack message is only sent when a device's status changes — not on every ping. On restart, an `initial` message is posted for **every** device (up or down) to re-establish baseline, so expect one Slack message per device each time the service starts.

### Escalation

If a device remains down past its `escalation_minutes` threshold, an `escalated` alert is sent. This repeats every `escalation_minutes` until the device recovers. The timer resets on monitor restart.

Escalation is only evaluated when the device is pinged, so actual timing is rounded up to the next ping cycle (e.g. a standard device with `escalation_minutes: 7` escalates at ~10 min with the default 300s interval).

### Infra Failure

If more than 80% of devices in a single cycle are down, one `infra_failure` alert is sent instead of individual `went_down` alerts. This prevents alert storms during a network outage.

- The alert is sent **once** when the outage starts. Individual alerts (down, up, escalation) are suppressed while it lasts.
- When the tier drops back to 80% or less down, one `infra_recovered` summary is sent listing any devices still down. Those devices keep their original down time and escalate normally afterwards.
- The check runs **per tier**: the CRITICAL and STANDARD threads each evaluate only their own devices.
- Exactly 80% does not trigger it (e.g. 4/5 down = individual alerts, 5/5 down = infra failure).
- A tier needs at least 2 devices. A single device going down is always a normal `went_down`.

## Events

| Event | Trigger |
|---|---|
| `initial` | Device state on monitor startup |
| `went_down` | Device transitioned from up → down |
| `went_up` | Device transitioned from down → up |
| `escalated` | Device still down past escalation threshold |
| `infra_failure` | >80% of a tier's devices down in same cycle (sent once) |
| `infra_recovered` | Tier back to ≤80% down after an infra failure |

## Slack Message Formats

Messages use Slack `mrkdwn` (bold via `*...*`) and emoji shortcodes, rendered here as they appear in Slack. Status emoji: 🟢 up, 🔴 down.

**Device Down** (same layout for `initial`, labelled *Initial State*):
```
🔴 Device Down — Site A Power Sensor (`10.0.0.1`)
Status: DOWN | Latency: N/A | 2026-04-19T10:39:38Z
```

**Device Up:**
```
🟢 Device Up — Site A Power Sensor (`10.0.0.1`)
Status: UP | Latency: 1.42ms | 2026-04-19T10:45:12Z
```

**Escalation:**
```
🔴 Escalation — Site A Power Sensor (`10.0.0.1`)
Down for: 10 min | Since: 2026-04-19T10:30:00Z | 2026-04-19T10:40:00Z
```

**Infrastructure Failure** (lists only devices in the affected tier):
```
🚨 Infrastructure Failure — 3/3 devices unreachable
Affected: Device 1, Device 2, Device 3 | 2026-04-19T10:39:38Z
```

**Infrastructure Recovered:**
```
🟢 Infrastructure Recovered — 2/3 devices reachable
Still down: Device 1 | 2026-04-19T10:47:02Z
```

## Configuration

### `config/devices.json`

```json
{
  "devices": [
    {
      "id": "power_sensor",
      "name": "Site A Power Sensor",
      "ip": "10.0.0.1",
      "type": "Power Sensor",
      "importance": "standard",
      "escalation_minutes": 30
    }
  ]
}
```

| Field | Description |
|---|---|
| `id` | Unique identifier used internally |
| `name` | Human-readable name shown in Slack messages |
| `ip` | IP or hostname to ping |
| `type` | Free-text label (e.g. `Server`, `Router`). Informational only, not used by the code |
| `importance` | `critical` (60s) or `standard` (300s) |
| `escalation_minutes` | Minutes before first escalation alert; repeats at same interval. Omit to disable escalation |

### Environment Variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `SLACK_BOT_TOKEN` | — | Bot token from Slack App (`xoxb-...`) |
| `SLACK_CHANNEL` | — | Channel name (e.g. `#alerts`) or channel ID (e.g. `C0123456789`) |
| `SEND_TIMEOUT` | `5` | HTTP request timeout in seconds |
| `CRITICAL_INTERVAL` | `60` | Ping interval for critical devices (seconds) |
| `STANDARD_INTERVAL` | `300` | Ping interval for standard devices (seconds) |
| `PING_TIMEOUT` | `2` | ICMP ping timeout in seconds |
| `DEVICES_CONFIG` | `config/devices.json` | Path to device config |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### `.env` Setup

```bash
cp .env.example .env
```

Only `SLACK_BOT_TOKEN` and `SLACK_CHANNEL` need filling in. Everything else in the template is set to its default.

## Slack App Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app
2. Under **OAuth & Permissions**, add the `chat:write` Bot Token Scope
3. Install the app to your workspace
4. Copy the **Bot User OAuth Token** (`xoxb-...`) into your `.env`
5. Invite the bot to your channel: `/invite @your-bot-name`

## Local Testing

```bash
# 1. Create and activate venv
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create .env and set SLACK_BOT_TOKEN + SLACK_CHANNEL
cp .env.example .env

# 4. Run
python run.py
```

If `SLACK_BOT_TOKEN` or `SLACK_CHANNEL` is not set, the monitor runs in **dry-run mode** — all alerts are logged to the console instead of sent to Slack.

Slack API errors (e.g. `not_in_channel`, `invalid_auth`, `channel_not_found`) are logged at `ERROR` level and do not stop the monitor.

### Windows

Double-click `start.bat` (or run it from `cmd`). It creates `venv\` on first run, installs dependencies, and starts the monitor. Copy `.env.example` to `.env` in the project root and fill it in first.


## Known Limitations

- **No escalation during infra failure.** While an infra failure is active, escalation alerts are suppressed for that tier. Escalation resumes after `infra_recovered`.
- **State is not persisted.** A restart clears all state and escalation timers, and re-posts `initial` for every device.
