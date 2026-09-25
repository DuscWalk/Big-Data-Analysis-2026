import json
from pathlib import Path
import shutil
import tempfile
import unittest

from quality_fixture import computed, report
from movielens_agent.contracts import ArtifactRef
from movielens_agent.storage.cleaned import CleanedDataset
from movielens_agent.workflows.governance import make_artifact


class CleanedReaderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.export = self.root / "producer"
        self.export.mkdir()
        value = report("producer")
        _, _, records = computed()
        self.paths = []
        for table in ("users", "movies", "ratings"):
            path = self.export / (table + ".jsonl")
            self.paths.append(path)
            rows = [item["data"] | {"source_ref": item["source_ref"]} for item in records if item["table"] == table]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.summary = {"input_ref": value["input_ref"], "configuration": value["configuration"],
                        "config_refs": value["config_refs"], "format": "jsonl", "encoding": "utf-8",
                        "rows": {key: item["rows"] for key, item in value["after"]["tables"].items()},
                        "splits": value["after"]["splits"], "split_materialization": "filter-by-timestamp",
                        "usable_for_training": True}
        self.manifest = self.export / "dataset-manifest.json"
        self.save_manifest()

    def save_manifest(self):
        value = make_artifact("producer", "cleaned", "cleaned_dataset", self.paths, self.summary)
        self.ref = ArtifactRef.model_validate(value["ref"])
        self.manifest.write_text(json.dumps(value))

    def dataset(self, path=None, ref=None, task="producer"):
        return CleanedDataset(path or self.manifest, expected_ref=ref or self.ref, expected_task_id=task)

    def test_portable_copy_and_exact_inclusive_time_boundaries(self):
        copied = self.root / "teammate"
        shutil.copytree(self.export, copied)
        shutil.rmtree(self.export)
        dataset = self.dataset(copied / "dataset-manifest.json")
        self.assertEqual(dataset.verify(), self.ref.model_dump())
        self.assertEqual([r["timestamp"] for r in dataset.ratings("train")], [975628799])
        self.assertEqual([r["timestamp"] for r in dataset.ratings("validation")], [978307199])
        self.assertEqual([r["timestamp"] for r in dataset.ratings("test")], [978307200])
        self.assertEqual(next(dataset.entities("users"))["zip_code"], "00100")
        with self.assertRaises(ValueError):
            dataset.entities("ratings")
        with self.assertRaises(ValueError):
            dataset.ratings("all")

    def test_changed_file_rejected_before_yield_even_after_earlier_verification(self):
        dataset = self.dataset()
        dataset.verify()
        with (self.export / "ratings.jsonl").open("a") as stream:
            stream.write("{}\n")
        with self.assertRaisesRegex(ValueError, "Cleaned file changed"):
            next(dataset.ratings("train"))

    def test_manifest_tampering_or_wrong_pinned_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            self.dataset(task="other")
        with self.assertRaises(ValueError):
            self.dataset(ref=self.ref.model_copy(update={"version": "latest"}))
        value = json.loads(self.manifest.read_text())
        value["summary"]["configuration"]["split"]["train_end"] += 1
        self.manifest.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "pinned version"):
            self.dataset()

    def test_missing_files_and_inconsistent_declared_partition_counts_fail(self):
        self.summary["splits"] = {"train": 2, "validation": 0, "test": 1}
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "Partition count"):
            list(self.dataset().ratings("train"))
        (self.export / "users.jsonl").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.dataset().verify()

    def test_not_usable_dataset_cannot_be_silently_used_for_training(self):
        self.summary["usable_for_training"] = False
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "not marked usable"):
            self.dataset().ratings("train")


if __name__ == "__main__":
    unittest.main()
