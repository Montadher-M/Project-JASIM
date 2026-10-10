import logging

logger = logging.getLogger(__name__)

INFRA_FAILURE_THRESHOLD = 0.80  # >80% down triggers infra alert
INFRA_MIN_DEVICES = 2  # a single device going down is a normal outage, not infra


def is_infra_failure(payloads: list) -> bool:
    """Return True if more than 80% of devices in a cycle are down (needs at least 2 devices)."""
    if len(payloads) < INFRA_MIN_DEVICES:
        return False
    down = sum(1 for p in payloads if p["status"] == "down")
    ratio = down / len(payloads)
    if ratio > INFRA_FAILURE_THRESHOLD:
        logger.warning(f"Infra failure threshold hit: {down}/{len(payloads)} devices down ({ratio:.0%})")
        return True
    return False
