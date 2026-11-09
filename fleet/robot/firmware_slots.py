"""A/B firmware slots: the data model behind safe over-the-air updates.

The robot always runs from the *active* slot. An update is written to the
*inactive* slot, so the running firmware is never touched while downloading.
The robot then boots into the new slot on trial; until that trial is
committed, the old slot is intact and rollback is just booting it again.
"""

from __future__ import annotations

from fleet.protocol import SLOT_NAMES


class FirmwareSlots:
    """Pure state, no threads or timing. Owned by the firmware updater thread."""

    def __init__(self, initial_version: str):
        self._versions: dict[str, str | None] = {name: None for name in SLOT_NAMES}
        self._versions["A"] = initial_version
        self._active = "A"
        self._previous_active: str | None = None  # only set during a trial boot

    @property
    def active_slot(self) -> str:
        return self._active

    @property
    def inactive_slot(self) -> str:
        return "B" if self._active == "A" else "A"

    @property
    def active_version(self) -> str:
        version = self._versions[self._active]
        assert version is not None  # the active slot always holds bootable firmware
        return version

    @property
    def inactive_version(self) -> str | None:
        return self._versions[self.inactive_slot]

    @property
    def in_trial(self) -> bool:
        return self._previous_active is not None

    def stage(self, version: str) -> None:
        """Write a new image into the inactive slot. The active slot is never touched."""
        self._require_not_in_trial()
        self._versions[self.inactive_slot] = version

    def boot_into_staged(self) -> None:
        """Switch the active slot to the staged image, starting a trial."""
        self._require_not_in_trial()
        if self.inactive_version is None:
            raise RuntimeError("nothing staged to boot into")
        self._previous_active = self._active
        self._active = self.inactive_slot

    def commit(self) -> None:
        """Trial passed: the new slot stays active. The old slot keeps the previous
        version as a fallback until the next update overwrites it."""
        self._require_in_trial()
        self._previous_active = None

    def rollback(self) -> None:
        """Trial failed: boot the previous slot again. The bad image stays in the
        inactive slot until the next update overwrites it."""
        self._require_in_trial()
        assert self._previous_active is not None
        self._active = self._previous_active
        self._previous_active = None

    def _require_in_trial(self) -> None:
        if not self.in_trial:
            raise RuntimeError("no trial boot in progress")

    def _require_not_in_trial(self) -> None:
        if self.in_trial:
            raise RuntimeError("a trial boot is in progress")
