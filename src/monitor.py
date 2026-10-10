import os
import json
import time
import random
import platform
import threading
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"

from src.sender import Sender
from src.detector import is_infra_failure

logger = logging.getLogger(__name__)

CRITICAL_INTERVAL = int(os.getenv("CRITICAL_INTERVAL", "60"))
STANDARD_INTERVAL = int(os.getenv("STANDARD_INTERVAL", "300"))
JITTER_FACTOR = 0.10  # ±10% spread on every interval
PING_TIMEOUT = int(os.getenv("PING_TIMEOUT", "2"))
CONFIG_PATH = Path(os.getenv("DEVICES_CONFIG", "config/devices.json"))
INFRA_KEY = "__infra__"  # per-thread flag in state: True while infra failure is active


def load_devices() -> list:
    with open(CONFIG_PATH) as f:
        return json.load(f)["devices"]


def _parse_latency(output: str) -> float | None:
    """Parse latency from ping output. Handles Linux (time=1.23 ms) and Windows (time=14ms, time<1ms)."""
    for line in output.splitlines():
        for sep in ("time=", "time<"):
            if sep in line:
                raw = line.split(sep)[1].split()[0]
                raw = raw.replace("ms", "").strip()
                try:
                    return float(raw)
                except ValueError:
                    pass
    return None


def ping(ip: str) -> dict:
    """
    Send a single ICMP ping. Cross-platform (Linux + Windows).
    Returns {"status": "up"|"down", "latency_ms": float|None}.
    """
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(PING_TIMEOUT * 1000), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(PING_TIMEOUT), ip]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PING_TIMEOUT + 2,
        )
        if result.returncode == 0:
            return {"status": "up", "latency_ms": _parse_latency(result.stdout)}
    except subprocess.TimeoutExpired:
        logger.warning(f"Ping timed out: {ip}")
    except Exception as e:
        logger.warning(f"Ping error for {ip}: {e}")

    return {"status": "down", "latency_ms": None}


def build_payload(device: dict, ping_result: dict, attempt: int = 1) -> dict:
    return {
        "device_id": device["id"],
        "name": device["name"],
        "ip": device["ip"],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": ping_result["status"],
        "latency_ms": ping_result["latency_ms"],
        "attempt": attempt,
    }


def _sync_state(payloads: list, state: dict, now: datetime):
    """Record current status without alerting. Keeps down_since/last_escalated for devices already down."""
    for payload in payloads:
        entry = state.get(payload["device_id"], {})
        if payload["status"] == "down":
            was_down = entry.get("status") == "down"
            state[payload["device_id"]] = {
                "status": "down",
                "down_since": entry.get("down_since") if was_down and entry.get("down_since") else now,
                "last_escalated": entry.get("last_escalated") if was_down else None,
            }
        else:
            state[payload["device_id"]] = {"status": "up", "down_since": None, "last_escalated": None}


def run_cycle(devices: list, sender: Sender, label: str, state: dict):
    """Ping all devices, send on state change or escalation. Infra failure overrides individual logic."""
    payloads = []
    device_map = {d["id"]: d for d in devices}

    for device in devices:
        result = ping(device["ip"])
        payload = build_payload(device, result)
        payloads.append(payload)
        logger.info(
            f"[{label}] [{payload['status'].upper():4}] {device['name']} ({device['ip']})"
            + (f" {payload['latency_ms']}ms" if payload["latency_ms"] is not None else "")
        )

    now = datetime.now(timezone.utc)
    infra_active = state.get(INFRA_KEY, False)

    if is_infra_failure(payloads):
        if not infra_active:
            sender.send_infra_alert(payloads)
            state[INFRA_KEY] = True
        else:
            logger.info(f"[{label}] Infra failure ongoing — alert suppressed")
        _sync_state(payloads, state, now)
        return

    if infra_active:
        # One summary instead of a went_up per device; still-down devices keep their down_since and escalate later
        logger.info(f"[{label}] Infra failure cleared")
        sender.send_infra_recovery(payloads)
        state[INFRA_KEY] = False
        _sync_state(payloads, state, now)
        return

    for payload in payloads:
        device_id = payload["device_id"]
        device = device_map[device_id]
        entry = state.get(device_id, {})
        previous = entry.get("status")
        current = payload["status"]

        if current != previous:
            if previous is None:
                event = "initial"
            elif current == "down":
                event = "went_down"
            else:
                event = "went_up"
            logger.info(
                f"[{label}] State change [{device_id}]: {previous or 'unknown'} → {current} ({event})"
            )
            sender.send({**payload, "event": event})
            down_since = now if current == "down" else None
            state[device_id] = {"status": current, "down_since": down_since, "last_escalated": None}

        elif current == "down":
            down_since = entry.get("down_since")
            last_escalated = entry.get("last_escalated")
            escalation_minutes = device.get("escalation_minutes")

            if escalation_minutes and down_since:
                elapsed_min = (now - down_since).total_seconds() / 60
                since_last = (now - last_escalated).total_seconds() / 60 if last_escalated else elapsed_min
                if elapsed_min >= escalation_minutes and since_last >= escalation_minutes:
                    down_duration_min = round(elapsed_min)
                    logger.warning(
                        f"[{label}] ESCALATION [{device_id}]: down for {down_duration_min}min"
                    )
                    sender.send({
                        **payload,
                        "event": "escalated",
                        "down_since": down_since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "down_duration_min": down_duration_min,
                    })
                    state[device_id]["last_escalated"] = now


def jittered_sleep(interval: int, stop_event: threading.Event):
    """Sleep for interval ± JITTER_FACTOR, interruptible by stop_event."""
    delta = interval * JITTER_FACTOR
    sleep_for = interval + random.uniform(-delta, delta)
    stop_event.wait(timeout=sleep_for)


def schedule_loop(
    label: str,
    devices: list,
    interval: int,
    sender: Sender,
    stop_event: threading.Event,
):
    logger.info(f"[{label}] Scheduler started — {len(devices)} devices, interval={interval}s")
    state: dict = {}
    while not stop_event.is_set():
        start = time.monotonic()
        try:
            run_cycle(devices, sender, label, state)
        except Exception as e:
            logger.error(f"[{label}] Cycle crashed: {e}", exc_info=True)
        elapsed = time.monotonic() - start
        remaining = max(0, interval - elapsed)
        # Sleep for the remaining time (with jitter), minus how long the cycle took
        delta = remaining * JITTER_FACTOR
        sleep_for = remaining + random.uniform(-delta, delta)
        stop_event.wait(timeout=max(0, sleep_for))


class MonitorService:
    def __init__(self):
        self.sender = Sender()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self):
        devices = load_devices()
        critical = [d for d in devices if d.get("importance") == "critical"]
        standard = [d for d in devices if d.get("importance") != "critical"]

        logger.info(f"Loaded {len(devices)} devices: {len(critical)} critical, {len(standard)} standard")

        if critical:
            self._spawn("CRITICAL", critical, CRITICAL_INTERVAL)
        if standard:
            self._spawn("STANDARD", standard, STANDARD_INTERVAL)

        if not self.threads:
            logger.error("No devices loaded. Check config/devices.json.")
            return

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Interrupt received — shutting down")
            self.stop()

    def stop(self):
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=10)
        logger.info("Monitor stopped")

    def _spawn(self, label: str, devices: list, interval: int):
        t = threading.Thread(
            target=schedule_loop,
            args=(label, devices, interval, self.sender, self.stop_event),
            daemon=True,
            name=f"monitor-{label.lower()}",
        )
        self.threads.append(t)
        t.start()
