"""Firmware artifacts the server can roll out to robots."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Iterable

ARTIFACT_SIZE_BYTES = 64 * 1024  # stand-in for a real image


@dataclass(frozen=True)
class Artifact:
    version: str
    payload: bytes
    sha256: str  # hex digest robots verify after downloading


def make_artifact(version: str, size: int = ARTIFACT_SIZE_BYTES) -> Artifact:
    """A fake firmware image. Deterministic per version, so every server run
    serves identical bytes (and an identical checksum) for the same version."""
    payload = random.Random(f"firmware-{version}").randbytes(size)
    return Artifact(version, payload, hashlib.sha256(payload).hexdigest())


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class FirmwareCatalog:
    def __init__(self, artifacts: Iterable[Artifact]):
        self._artifacts = {a.version: a for a in artifacts}

    def get(self, version: str) -> Artifact | None:
        return self._artifacts.get(version)

    def versions(self) -> list[str]:
        """All known versions, oldest first."""
        return sorted(self._artifacts, key=_version_key)

    def latest(self) -> str | None:
        versions = self.versions()
        return versions[-1] if versions else None


def default_catalog() -> FirmwareCatalog:
    return FirmwareCatalog([make_artifact("2.0.0")])
