"""Wire format shared by the robot agents (producers) and the server (consumer)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum, IntEnum
from typing import Any


class StatusCode(IntEnum):
    IDLE = 0
    RUNNING = 1
    CHARGING = 2
    ERROR = 3
    PAUSED = 4
    # Firmware update lifecycle (A/B slots). The arm is out of service for all of these.
    DOWNLOADING = 5   # fetching + verifying the artifact into the inactive slot
    REBOOTING = 6     # restarting into the freshly written slot
    TRIAL_BOOT = 7    # running the new firmware on probation, not yet committed
    ROLLING_BACK = 8  # trial failed; rebooting back into the previous slot


UPDATE_STATUSES = frozenset({
    StatusCode.DOWNLOADING, StatusCode.REBOOTING, StatusCode.TRIAL_BOOT, StatusCode.ROLLING_BACK,
})


class UpdateResult(str, Enum):
    """Outcome of the robot's most recent firmware update attempt."""
    NONE = "none"                        # never attempted
    COMMITTED = "committed"              # trial passed; new firmware is permanent
    ROLLED_BACK = "rolled_back"          # trial failed; back on the previous firmware
    DOWNLOAD_FAILED = "download_failed"  # never got a valid artifact; nothing changed


SLOT_NAMES = ("A", "B")


class CommandType(str, Enum):
    """Commands the server can push to a robot over the websocket."""
    PAUSE = "pause"
    RESUME = "resume"
    UPDATE_FIRMWARE = "update_firmware"


@dataclass(frozen=True)
class Command:
    type: CommandType
    version: str | None = None  # update_firmware only: the version to install
    sha256: str | None = None   # update_firmware only: expected artifact checksum

    def __post_init__(self) -> None:
        if self.type == CommandType.UPDATE_FIRMWARE and not (self.version and self.sha256):
            raise ValueError("update_firmware requires a version and sha256")


def hello_message(robot_id: str) -> dict[str, Any]:
    """First message a robot sends after connecting, so the server knows who it is."""
    return {"type": "hello", "robot_id": robot_id}


def parse_hello(data: Any) -> str:
    """Returns the robot_id. Raises ValueError if the message isn't a valid hello."""
    if not isinstance(data, dict) or data.get("type") != "hello":
        raise ValueError("expected a hello message")
    robot_id = data.get("robot_id")
    if not isinstance(robot_id, str) or not robot_id:
        raise ValueError("hello must include a robot_id")
    return robot_id


def command_message(command: Command) -> dict[str, Any]:
    message: dict[str, Any] = {"type": "command", "command": command.type.value}
    if command.version is not None:
        message["version"] = command.version
    if command.sha256 is not None:
        message["sha256"] = command.sha256
    return message


def parse_command_message(data: Any) -> Command:
    """Raises ValueError if the message isn't a valid command."""
    if not isinstance(data, dict) or data.get("type") != "command":
        raise ValueError("expected a command message")
    try:
        command_type = CommandType(data.get("command"))
    except ValueError:
        raise ValueError(f"unknown command: {data.get('command')!r}") from None
    version = _optional(data, "version", str, None)
    sha256 = _optional(data, "sha256", str, None)
    return Command(command_type, version, sha256)


@dataclass(frozen=True)
class Heartbeat:
    robot_id: str
    seq: int  # per-robot counter; lets the server detect gaps / duplicates later
    firmware_version: str  # the version currently *running* (during a trial boot, the new one)
    battery_level: float  # percent, 0-100
    status_code: int  # StatusCode value
    sent_at: float  # robot-side wall clock (epoch seconds)
    # Firmware slot info. Optional on the wire (with defaults) so robots on older
    # firmware that don't report it still validate.
    active_slot: str = "A"
    inactive_slot_version: str | None = None  # what the standby slot holds, if anything
    last_update_result: str = UpdateResult.NONE.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "Heartbeat":
        """Build a Heartbeat from untrusted JSON. Raises ValueError if invalid."""
        if not isinstance(data, dict):
            raise ValueError("heartbeat must be a JSON object")

        robot_id = _require(data, "robot_id", str)
        if not robot_id:
            raise ValueError("robot_id must not be empty")

        seq = _require(data, "seq", int)
        firmware_version = _require(data, "firmware_version", str)
        battery_level = float(_require(data, "battery_level", (int, float)))
        status_code = _require(data, "status_code", int)
        sent_at = float(_require(data, "sent_at", (int, float)))
        active_slot = _optional(data, "active_slot", str, "A")
        inactive_slot_version = _optional(data, "inactive_slot_version", str, None)
        last_update_result = _optional(data, "last_update_result", str, UpdateResult.NONE.value)

        if not 0.0 <= battery_level <= 100.0:
            raise ValueError("battery_level must be between 0 and 100")
        try:
            StatusCode(status_code)
        except ValueError:
            raise ValueError(f"unknown status_code: {status_code}") from None
        if active_slot not in SLOT_NAMES:
            raise ValueError(f"unknown active_slot: {active_slot!r}")
        if last_update_result not in {r.value for r in UpdateResult}:
            raise ValueError(f"unknown last_update_result: {last_update_result!r}")

        return cls(robot_id, seq, firmware_version, battery_level, status_code, sent_at,
                   active_slot, inactive_slot_version, last_update_result)


def _require(data: dict, key: str, expected_type: type | tuple[type, ...]) -> Any:
    if key not in data:
        raise ValueError(f"missing field: {key}")
    value = data[key]
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, expected_type):
        raise ValueError(f"field {key} has wrong type")
    return value


def _optional(data: dict, key: str, expected_type: type, default: Any) -> Any:
    """Like _require, but a missing (or null) field yields `default`."""
    if data.get(key) is None:
        return default
    return _require(data, key, expected_type)
