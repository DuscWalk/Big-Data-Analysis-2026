"""Immutable, exact-version dataset registration for the first development slice."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..contracts import ArtifactRef, DatasetManifest


class CatalogConflict(ValueError):
    pass


class DatasetCatalog:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS datasets ("
                "artifact_id TEXT NOT NULL, version TEXT NOT NULL, manifest TEXT NOT NULL, "
                "PRIMARY KEY (artifact_id, version))"
            )
            with connection:
                yield connection
        finally:
            connection.close()

    def register(self, manifest: DatasetManifest) -> None:
        payload = manifest.model_dump_json()
        ref = manifest.ref
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO datasets VALUES (?, ?, ?)",
                (ref.artifact_id, ref.version, payload),
            )
            existing = connection.execute(
                "SELECT manifest FROM datasets WHERE artifact_id = ? AND version = ?",
                (ref.artifact_id, ref.version),
            ).fetchone()[0]
            if DatasetManifest.model_validate_json(existing) != manifest:
                raise CatalogConflict("This dataset version already has a different manifest.")

    def get(self, ref: ArtifactRef) -> DatasetManifest:
        if not self.path.is_file():
            raise KeyError("Dataset catalog does not exist.")
        # Queries are read-only and do not create or alter catalog schema.
        uri = self.path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            row = connection.execute(
                "SELECT manifest FROM datasets WHERE artifact_id = ? AND version = ?",
                (ref.artifact_id, ref.version),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise KeyError("The exact dataset version is not registered.")
        return DatasetManifest.model_validate_json(row[0])

    def versions(self, artifact_id: str) -> list[ArtifactRef]:
        """Return exact registered versions without creating a missing catalog."""
        if not self.path.is_file():
            raise KeyError("Dataset catalog does not exist.")
        uri = self.path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            rows = connection.execute(
                "SELECT artifact_id, version FROM datasets WHERE artifact_id = ? ORDER BY version",
                (artifact_id,),
            ).fetchall()
        finally:
            connection.close()
        return [ArtifactRef(artifact_id=row[0], version=row[1]) for row in rows]
