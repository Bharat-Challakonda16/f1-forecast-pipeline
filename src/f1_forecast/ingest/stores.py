"""Storage backends for downloaded raw files.

The store doubles as the "already downloaded" list: a file that exists is never fetched again.

* ``LocalStore``: a folder on disk (local development, small samples).
* ``VolumeStore``: a Unity Catalog Volume in Databricks (CI runs). Nothing touches the
  machine running the job beyond memory, so laptops and CI runners stay clean.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from f1_forecast.ingest.http import LocalStore, Store

__all__ = ["LocalStore", "VolumeStore", "make_store"]


@dataclass
class VolumeStore:
    """Files under ``/Volumes/<catalog>/<schema>/<volume>/`` via the Databricks Files API.

    Credentials come from the standard ``DATABRICKS_HOST`` / ``DATABRICKS_TOKEN`` environment
    variables (GitHub Actions secrets) or a profile, handled by ``databricks-sdk``.
    """

    root: str
    client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.root.startswith("/Volumes/"):
            raise ValueError(f"Volume root must start with /Volumes/: {self.root!r}")
        self.root = self.root.rstrip("/")
        if self.client is None:
            from databricks.sdk import WorkspaceClient

            self.client = WorkspaceClient()

    def _path(self, key: str) -> str:
        return f"{self.root}/{key}"

    def exists(self, key: str) -> bool:
        from databricks.sdk.errors import NotFound

        try:
            self.client.files.get_metadata(self._path(key))
            return True
        except NotFound:
            return False

    def read(self, key: str) -> bytes:
        return self.client.files.download(self._path(key)).contents.read()

    def write(self, key: str, data: bytes) -> None:
        # The Files API creates parent directories and replaces the file in one call.
        self.client.files.upload(self._path(key), io.BytesIO(data), overwrite=True)


def make_store(kind: str, location: str) -> Store:
    if kind == "local":
        return LocalStore(Path(location))
    if kind == "volume":
        return VolumeStore(location)
    raise ValueError(f"unknown store kind: {kind!r}")
