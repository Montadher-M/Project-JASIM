import os
import logging
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "")
SEND_TIMEOUT = int(os.getenv("SEND_TIMEOUT", "5"))

SLACK_API_URL = "https://slack.com/api/chat.postMessage"

_STATUS_EMOJI = {
    "up": ":large_green_circle:",
    "down": ":red_circle:",
}

_EVENT_LABEL = {
    "went_down": "Device Down",
    "went_up":   "Device Up",
    "initial":   "Initial State",
    "escalated": "Escalation",
}


def _format_message(payload: dict) -> str:
    event = payload.get("event", "")
    status = payload.get("status", "unknown")
    emoji = _STATUS_EMOJI.get(status, ":white_circle:")
    label = _EVENT_LABEL.get(event, event)
    name = payload.get("name", payload.get("device_id"))
    ip = payload.get("ip", "?")
    latency = payload.get("latency_ms")
    latency_str = f"{latency}ms" if latency is not None else "N/A"
    ts = payload.get("timestamp", "")

    if event == "escalated":
        duration = payload.get("down_duration_min", "?")
        down_since = payload.get("down_since", "")
        return (
            f"{emoji} *{label}* — {name} (`{ip}`)\n"
            f"Down for: *{duration} min* | Since: {down_since} | {ts}"
        )

    return (
        f"{emoji} *{label}* — {name} (`{ip}`)\n"
        f"Status: *{status.upper()}* | Latency: {latency_str} | {ts}"
    )


class Sender:
    def __init__(self):
        if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
            logger.warning("SLACK_BOT_TOKEN or SLACK_CHANNEL not set — running in dry-run mode (results logged only)")

    def _post(self, text: str):
        if not SLACK_BOT_TOKEN or not SLACK_CHANNEL:
            logger.info(f"[DRY RUN] {text}")
            return

        try:
            resp = requests.post(
                SLACK_API_URL,
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={"channel": SLACK_CHANNEL, "text": text},
                timeout=SEND_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                logger.error(f"Slack API error: {data.get('error')}")
            else:
                logger.debug(f"Slack sent to {SLACK_CHANNEL}")
        except requests.RequestException as e:
            logger.error(f"Slack send failed: {e}")

    def send(self, payload: dict):
        self._post(_format_message(payload))

    def send_infra_alert(self, payloads: list):
        down = [p for p in payloads if p["status"] == "down"]
        total = len(payloads)
        down_count = len(down)
        device_list = ", ".join(p["name"] for p in down)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        text = (
            f":rotating_light: *Infrastructure Failure* — {down_count}/{total} devices unreachable\n"
            f"Affected: {device_list} | {ts}"
        )
        self._post(text)

    def send_infra_recovery(self, payloads: list):
        down = [p for p in payloads if p["status"] == "down"]
        total = len(payloads)
        up_count = total - len(down)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        text = f":large_green_circle: *Infrastructure Recovered* — {up_count}/{total} devices reachable\n"
        if down:
            text += f"Still down: {', '.join(p['name'] for p in down)} | {ts}"
        else:
            text += ts
        self._post(text)
