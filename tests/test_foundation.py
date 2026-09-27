from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

from movielens_agent.cli import save_profile
from movielens_agent.contracts import ArtifactRef, DatasetManifest, ToolContext
from movielens_agent.storage.catalog import CatalogConflict, DatasetCatalog
from movielens_agent.tools.datasets import DescribeInput, register_dataset_tools
from movielens_agent.tools.registry import QueryTool, ToolRegistry


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "ml-1m"
        self.source.mkdir()
        for name, text in {
            "users": "1::F::18::4::00100\n", "movies": "1::Toy Story (1995)::Animation\n",
            "ratings": "1::1::5::946684800\n",
        }.items():
            (self.source / f"{name}.dat").write_text(text)
        self.db = self.root / "var/catalog.sqlite3"
        with redirect_stderr(io.StringIO()):
            self.result = save_profile(self.source, self.root / "var/run-1", self.db)
        self.catalog = DatasetCatalog(self.db)
        self.ref = ArtifactRef.model_validate(self.result["dataset_ref"])
        self.registry = ToolRegistry()
        register_dataset_tools(self.registry, self.catalog)
        self.context = ToolContext(session_id="test-session", call_id="test-call")

    def test_profile_catalog_and_real_query_tool_round_trip(self):
        response = self.registry.call("datasets.describe", {"dataset_ref": self.ref.model_dump()}, self.context)
        self.assertEqual(response.status, "completed")
        self.assertEqual(response.evidence, [self.ref])
        self.assertEqual({table["name"]: table["rows"] for table in response.data["tables"]},
                         {"users": 1, "movies": 1, "ratings": 1})
        self.assertEqual(response.call_id, "test-call")
        self.assertEqual(self.registry.describe()[0]["input_schema"]["additionalProperties"], False)

    def test_missing_version_and_invalid_arguments_are_distinct(self):
        missing = {"dataset_ref": {"artifact_id": "ml-1m.raw", "version": "missing"}}
        response = self.registry.call("datasets.describe", missing, self.context)
        self.assertEqual((response.status, response.error.code), ("rejected", "ARTIFACT_NOT_FOUND"))
        response = self.registry.call("datasets.describe", {"dataset_ref": self.ref.model_dump(),
                                                            "session_id": "override"}, self.context)
        self.assertEqual(response.error.code, "INVALID_ARGUMENTS")
        response = self.registry.call("not.implemented", {}, self.context)
        self.assertEqual(response.error.code, "TOOL_NOT_FOUND")

    def test_catalog_is_idempotent_and_versions_cannot_be_overwritten(self):
        manifest = self.catalog.get(self.ref)
        self.catalog.register(manifest)
        changed = manifest.model_copy(update={"source_directory": "/different"})
        with self.assertRaises(CatalogConflict):
            self.catalog.register(changed)
        self.assertEqual(self.catalog.get(self.ref), manifest)
        # New catalog instance reads persisted state, rather than in-memory cache.
        self.assertEqual(DatasetCatalog(self.db).get(self.ref), manifest)

    def test_missing_catalog_query_has_no_write_side_effect(self):
        missing = self.root / "never-created/catalog.sqlite3"
        with self.assertRaises(KeyError):
            DatasetCatalog(missing).get(self.ref)
        self.assertFalse(missing.parent.exists())

    def test_existing_output_and_source_directory_outputs_are_rejected(self):
        for output in [self.root / "var/run-1", self.source / "generated"]:
            with self.assertRaises((FileExistsError, ValueError)):
                save_profile(self.source, output, self.db)
        with self.assertRaises(ValueError):
            save_profile(self.source, self.root / "new-run", self.source / "catalog.sqlite3")
        self.assertFalse((self.source / "generated").exists())

    def test_query_execution_errors_do_not_claim_completion(self):
        def fail(arguments, context):
            raise OSError("deliberate fixture failure")
        self.registry.register(QueryTool("test.failure", "1", "Failure fixture", DescribeInput,
                                         DatasetManifest, fail))
        with self.assertLogs("movielens_agent.tools.registry", level="ERROR"):
            result = self.registry.call("test.failure", {"dataset_ref": self.ref.model_dump()}, self.context)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.code, "TOOL_EXECUTION_FAILED")
        self.assertIsNone(result.data)


if __name__ == "__main__":
    unittest.main()
