"""Turn stored heartbeats into the JSON shape the dashboard consumes."""

from __future__ import annotations

from fleet.protocol import UPDATE_STATUSES, StatusCode
from fleet.server.store import StoredHeartbeat


def robot_view(stored: StoredHeartbeat, now: float, offline_after_s: float,
               connected: bool) -> dict:
    """Online/offline is derived from how long ago we last heard from the robot,
    using the server's clock (robot clocks can't be trusted).

    `connected` is separate: it says whether we can currently push commands
    (websocket open), whereas `online` is about heartbeats.
    """
    hb = stored.heartbeat
    age_s = max(0.0, now - stored.received_at)
    return {
        "robot_id": hb.robot_id,
        "firmware_version": hb.firmware_version,
        "battery_level": hb.battery_level,
        "status_code": hb.status_code,
        "status": StatusCode(hb.status_code).name,
        "seq": hb.seq,
        "last_seen_age_s": round(age_s, 1),
        "online": age_s <= offline_after_s,
        "connected": connected,
        "active_slot": hb.active_slot,
        "inactive_slot_version": hb.inactive_slot_version,
        "last_update_result": hb.last_update_result,
        "updating": StatusCode(hb.status_code) in UPDATE_STATUSES,
    }


def log_entry_view(stored: StoredHeartbeat, late_after_s: float) -> dict:
    """One row of a robot's received-telemetry log.

    `delay_s` is how long after the robot sampled it the server received it. In
    normal operation that's tiny; telemetry that was buffered during an outage
    arrives in a burst with a large delay, and is flagged `late`.
    (Uses the robot's clock for sent_at, so it's only as accurate as that clock.)
    """
    hb = stored.heartbeat
    delay_s = max(0.0, stored.received_at - hb.sent_at)
    return {
        **hb.to_dict(),
        "status": StatusCode(hb.status_code).name,
        "received_at": stored.received_at,
        "delay_s": round(delay_s, 1),
        "late": delay_s > late_after_s,
    }


def fleet_view(latest: list[StoredHeartbeat], now: float, offline_after_s: float,
               connected_ids: set[str]) -> list[dict]:
    return [
        robot_view(stored, now, offline_after_s, stored.heartbeat.robot_id in connected_ids)
        for stored in latest
    ]
