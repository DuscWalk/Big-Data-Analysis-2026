import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import unittest

from movielens_agent.governance.config import GovernanceConfig
from movielens_agent.governance import streaming as mr
from governance_fixture import DATA, EXPECTED_DISPOSITIONS

CONFIG = GovernanceConfig.read(Path("configs/governance/default.json")).model_dump(mode="json")


def captured(function, *args, **kwargs):
    stream = io.StringIO()
    with redirect_stdout(stream):
        function(*args, **kwargs)
    return stream.getvalue().splitlines()


def envelopes(table, text):
    offset, rows = 0, []
    for line_number, text in enumerate(text.splitlines(keepends=True), 1):
        raw = text.encode("iso-8859-1")
        rows.append(mr.encode({"kind": "raw", "table": table,
            "raw_b64": base64.b64encode(raw).decode(),
            "source_ref": {"file_name": table + ".dat", "byte_offset": offset,
                           "line": line_number, "dataset_ref": {"artifact_id": "fixture", "version": "1"}}}))
        offset += len(raw)
    return rows


def job(rows, mode="score", parents=None, raw_parents=None):
    mapped = captured(mr.map_rows, rows, CONFIG, parents or {}, raw_parents or {}, mode)
    return captured(mr.reduce_rows, sorted(mapped), mode, CONFIG)


def index(rows):
    parents = {"users": {}, "movies": {}}
    for row in map(json.loads, rows):
        if row["kind"] == "parent":
            parents[row["table"]][str(row["id"])] = {"conflict": row["conflict"]}
    return parents


def aggregate(rows):
    mapped = captured(mr.aggregate_map, rows, CONFIG)
    return json.loads(captured(mr.aggregate_reduce, mapped, CONFIG)[0])


class GovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = {table: envelopes(table, content) for table, content in DATA.items()}
        before_parents = job(raw["users"] + raw["movies"])
        raw_index = index(before_parents)
        before_ratings = job(raw["ratings"], parents=raw_index)
        cls.before = aggregate(before_parents + before_ratings)
        parents = job(raw["users"] + raw["movies"], mode="clean")
        ratings = job(raw["ratings"], mode="clean", parents=index(parents), raw_parents=raw_index)
        clean = parents + ratings
        cls.cleaned = [json.loads(row) for row in clean]
        cls.after = aggregate(job(clean, parents=index(parents)) + clean)

    def test_hand_calculated_before_scores_include_malformed_and_conflicts(self):
        before = self.before["tables"]
        self.assertAlmostEqual(before["users"]["metrics"]["Accurate"]["score"], 100 * 17 / 18)
        self.assertAlmostEqual(before["ratings"]["metrics"]["Accurate"]["score"], 100 * 10 / 12)
        self.assertAlmostEqual(before["ratings"]["metrics"]["Complete"]["score"], 100 * 44 / 48)
        self.assertEqual(before["users"]["metrics"]["Unique"]["score"], 50)
        self.assertAlmostEqual(before["ratings"]["metrics"]["Unique"]["score"], 100 * 8 / 12)
        self.assertAlmostEqual(before["users"]["metrics"]["Consistent"]["score"], 100 / 6)
        self.assertEqual(before["ratings"]["metrics"]["Consistent"]["score"], 0)
        self.assertEqual(self.before["overall"]["Up-to-date"]["score"], 0)

    def test_invalid_candidates_do_not_poison_valid_groups_and_counts_conserve(self):
        self.assertEqual(self.after["dispositions"], EXPECTED_DISPOSITIONS)
        for table, counts in EXPECTED_DISPOSITIONS.items():
            output = self.after["tables"][table]["rows"]
            self.assertEqual(self.before["tables"][table]["rows"],
                             output + counts.get("deduplicated", 0) + counts.get("quarantined", 0))
        for dimension in ("Accurate", "Complete", "Unique", "Consistent"):
            self.assertEqual(self.after["overall"][dimension]["score"], 100)
        self.assertEqual(self.after["overall"]["Up-to-date"]["score"], 0)

    def test_different_timestamp_events_survive_and_boundaries_are_inclusive(self):
        self.assertEqual(self.after["splits"], {"train": 1, "validation": 1, "test": 1})
        ratings = [row["data"] for row in self.cleaned if row["kind"] == "record" and row["table"] == "ratings"]
        self.assertEqual(len(ratings), 3)
        self.assertEqual(sum(row["movie_id"] == 2 for row in ratings), 2)
        self.assertTrue(all(row["user_id"] == 1 for row in ratings))

    def test_cascades_future_events_and_ambiguity_have_distinct_evidence(self):
        reasons = self.after["reasons"]["ratings"]
        self.assertEqual(reasons["R12_PARENT_REMOVED_USERS"], 1)
        self.assertEqual(reasons["R12_PARENT_REMOVED_MOVIES"], 1)
        self.assertEqual(reasons["R12_PARENT_MISSING_USERS"], 1)
        self.assertEqual(reasons["R10_HISTORICAL_RANGE"], 1)
        self.assertEqual(reasons["R14_VALID_KEY_CONFLICT"], 2)
        self.assertEqual(self.after["warnings"]["users"]["UNVERIFIED_ZIP_FORMAT"], 1)
        sample = self.after["samples"]["ratings/R15_DUPLICATE"][0]
        self.assertEqual(sample["source_ref"]["line"], 2)
        self.assertEqual(sample["retained_source_ref"]["line"], 1)

    def test_empty_input_is_not_perfect_and_optional_time_dimension_is_distinct(self):
        result = aggregate([])
        self.assertTrue(all(metric["score"] is None for metric in result["overall"].values()))
        self.assertEqual(result["tables"]["users"]["metrics"]["Up-to-date"]["status"], "not_applicable")
        self.assertEqual(result["tables"]["ratings"]["metrics"]["Up-to-date"]["status"], "not_evaluable")

    def test_text_and_leading_zip_zero_are_preserved(self):
        raw = envelopes("movies", "1::Léon (1994)::Drama\n")
        result = [json.loads(row) for row in job(raw, mode="clean")]
        record = next(row for row in result if row["kind"] == "record")
        self.assertEqual(record["data"]["title"], "Léon (1994)")
        user = next(row for row in self.cleaned if row["kind"] == "record"
                    and row["table"] == "users" and row["data"]["user_id"] == 1)
        self.assertEqual(user["data"]["zip_code"], "00100")

    def test_freshness_window_is_closed_and_future_is_not_fresh(self):
        reference = CONFIG["metrics"]["reference_time"]
        lower = reference - CONFIG["metrics"]["window_seconds"]
        for timestamp, expected in ((lower - 1, 0), (lower, 1), (reference, 1), (reference + 1, 0)):
            row = json.loads(envelopes("ratings", f"1::1::4::{timestamp}\n")[0])
            self.assertEqual(mr.evaluate(row, CONFIG)["counts"]["uptodate_passed"], expected)

    def test_reducer_result_is_independent_of_equal_key_input_order(self):
        raw = envelopes("users", "1::F::18::4::00100\n1::F::18::4::00100\n")
        forward = list(map(json.loads, job(raw, mode="clean")))
        reverse = list(map(json.loads, job(list(reversed(raw)), mode="clean")))
        key = lambda row: mr.encode(row)
        self.assertEqual(sorted(forward, key=key), sorted(reverse, key=key))


if __name__ == "__main__":
    unittest.main()
