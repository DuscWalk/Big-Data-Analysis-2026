"""Immutable governance schemes stored beside the tasks that consume them."""
from uuid import uuid4

from ..contracts import ArtifactRef
from ..governance.config import GovernanceConfig, canonical
from .tasks import TaskStore, now


class ConfigurationConflict(ValueError):
    pass


class GovernanceConfigurations(TaskStore):
    def __init__(self, path, default: GovernanceConfig):
        super().__init__(path)
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS governance_configurations ("
                         "version TEXT PRIMARY KEY, name TEXT NOT NULL, configuration TEXT NOT NULL, "
                         "created_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS governance_default ("
                         "id INTEGER PRIMARY KEY CHECK(id=1), version TEXT NOT NULL "
                         "REFERENCES governance_configurations(version), revision TEXT NOT NULL)")
        self.startup_ref = default.ref()
        self.register("默认方案", default)
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO governance_default VALUES (1,?,?)",
                         (default.ref()["version"], uuid4().hex))

    def default(self):
        with self.connect() as conn:
            return self._default(conn)

    def _default(self, conn):
        row = conn.execute("SELECT * FROM governance_default WHERE id=1").fetchone()
        return {"ref": {"artifact_id": "governance.configuration", "version": row["version"]},
                "revision": row["revision"]} if row else {"ref": self.startup_ref, "revision": None}

    @property
    def default_ref(self):
        return self.default()["ref"]

    def _item(self, row, default_ref=None):
        config = GovernanceConfig.model_validate_json(row["configuration"])
        if config.ref()["version"] != row["version"]:
            raise ValueError("治理方案内容与登记版本不一致。")
        return {"ref": config.ref(), "name": row["name"], "configuration": config.model_dump(mode="json"),
                "config_refs": config.refs(), "is_default": config.ref() == (default_ref or self.default_ref),
                "created_at": row["created_at"]}

    def register(self, name: str, configuration: GovernanceConfig, *, make_default=False, revision=None):
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("方案名称应为 1—80 个字符。")
        config = GovernanceConfig.model_validate(configuration.model_dump(mode="json"))
        version = config.ref()["version"]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if make_default:
                self._check_revision(conn, revision)
            conn.execute("INSERT OR IGNORE INTO governance_configurations VALUES (?,?,?,?)",
                         (version, name, canonical(config.model_dump(mode="json")), now()))
            if make_default:
                conn.execute("UPDATE governance_default SET version=?,revision=? WHERE id=1", (version, uuid4().hex))
            row = conn.execute("SELECT * FROM governance_configurations WHERE version=?", (version,)).fetchone()
            default = self._default(conn)
        return self._item(row, default["ref"])

    @staticmethod
    def _check_revision(conn, revision):
        row = conn.execute("SELECT revision FROM governance_default WHERE id=1").fetchone()
        if not revision or row is None or row[0] != revision:
            raise ConfigurationConflict("默认方案已更新，请重新读取后再设为默认。")

    def set_default(self, ref: ArtifactRef, revision):
        self.get(ref)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check_revision(conn, revision)
            conn.execute("UPDATE governance_default SET version=?,revision=? WHERE id=1", (ref.version, uuid4().hex))
        return self.get(ref)

    def get(self, ref: ArtifactRef):
        if ref.artifact_id != "governance.configuration":
            raise KeyError("治理方案未登记。")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM governance_configurations WHERE version=?", (ref.version,)).fetchone()
        if row is None:
            raise KeyError("治理方案未登记。")
        return self._item(row)

    def list(self, offset=0, limit=20):
        with self.connect() as conn:
            conn.execute("BEGIN")
            default = self._default(conn)
            rows = conn.execute("SELECT * FROM governance_configurations ORDER BY created_at DESC,version "
                                "LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
        return {"items": [self._item(row, default["ref"]) for row in rows[:limit]], "has_more": len(rows) > limit,
                "offset": offset, "default_ref": default["ref"], "default": default}
