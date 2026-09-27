from datetime import datetime, timezone
import hashlib
import tempfile
from pathlib import Path
import unittest

from movielens_agent.governance.profile import profile_dataset

AS_OF = datetime(2026, 9, 24, tzinfo=timezone.utc)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "ml-1m"
        self.source.mkdir()
        self.write("users", "1::F::18::4::00100\n2::M::25::7::12345\n")
        self.write("movies", "1::Léon (1994)::Crime\n2::Toy Story (1995)::Animation\n")
        self.write("ratings", "1::1::5::946684800\n")

    def write(self, name, text):
        (self.source / f"{name}.dat").write_bytes(text.encode("iso-8859-1"))

    def profile(self):
        return profile_dataset(self.source, as_of=AS_OF)

    def test_no_final_newline_latin1_fingerprint_and_read_only(self):
        self.write("movies", "1::Léon (1994)::Crime")
        before = {p.name: p.read_bytes() for p in self.source.iterdir()}
        result = self.profile()
        self.assertEqual(result["tables"]["movies"]["rows"], 1)
        for table in result["manifest"]["tables"]:
            original = before[table["file_name"]]
            self.assertEqual(table["sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(table["size_bytes"], len(original))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.source.iterdir()})

    def test_same_pair_different_time_is_not_duplicate_event(self):
        self.write("ratings", "1::1::5::946684800\n1::1::5::946684801\n"
                   "1::1::5::946684800\n1::1::4::946684800\n")
        ratings = self.profile()["tables"]["ratings"]
        self.assertEqual(ratings["findings"]["exact_duplicate_extra_row"], 1)
        self.assertEqual(ratings["business_keys"], {
            "distinct": 2, "rows_without_valid_key": 0, "repeated_key_groups": 1,
            "repeated_key_extra_rows": 2, "conflicting_key_groups": 1,
            "rows_in_conflicting_groups": 3})

    def test_conflicting_parent_and_missing_references(self):
        self.write("users", "1::F::18::4::00100\n1::M::18::4::00100\n")
        self.write("ratings", "1::1::5::946684800\n99::88::3::946684801\n")
        tables = self.profile()["tables"]
        self.assertEqual(tables["users"]["business_keys"]["rows_in_conflicting_groups"], 2)
        findings = tables["ratings"]["findings"]
        self.assertEqual(findings["reference_to_conflicting_users"], 1)
        self.assertEqual(findings["reference_missing_from_parseable_users"], 1)
        self.assertEqual(findings["reference_missing_from_parseable_movies"], 1)

    def test_malformed_blank_missing_and_out_of_range_rows(self):
        self.write("ratings", "\n1::2\n1::1::::946684800\n1::1::6::-1\n1_0::1::5::99999999999999999999\n")
        ratings = self.profile()["tables"]["ratings"]
        self.assertEqual((ratings["rows"], ratings["parsed_rows"], ratings["unparsed_rows"]), (5, 3, 2))
        self.assertEqual(ratings["findings"]["missing_field"], 1)
        self.assertEqual(ratings["findings"]["invalid_rating"], 2)
        self.assertEqual(ratings["findings"]["invalid_timestamp"], 2)
        self.assertEqual(ratings["business_keys"]["rows_without_valid_key"], 4)
        sample = ratings["samples"]["wrong_field_count"][0]
        self.assertEqual((sample["line"], sample["byte_offset"]), (2, 1))

    def test_observations_do_not_change_source_or_keys(self):
        self.write("users", "1::F::18::4::12\n 1 ::F::018::04::12\n")
        self.write("movies", "1::No release year::Drama|Drama\n")
        result = self.profile()["tables"]
        self.assertEqual(result["users"]["findings"]["unusual_zip_format"], 2)
        self.assertEqual(result["users"]["business_keys"]["conflicting_key_groups"], 0)
        self.assertEqual(result["users"]["business_keys"]["repeated_key_extra_rows"], 1)
        self.assertEqual(result["movies"]["findings"]["duplicate_genre_label"], 1)

    def test_future_times_are_reported_without_selecting_cutoffs(self):
        self.write("ratings", "1::1::5::946684800\n1::1::5::4102444800\n")
        result = self.profile()
        ratings = result["tables"]["ratings"]
        self.assertEqual(ratings["findings"]["timestamp_after_profile_time"], 1)
        self.assertEqual(ratings["time_distribution"]["by_year_utc"], {2000: 1, 2100: 1})
        self.assertNotIn("train_end", result["manifest"])

    def test_empty_files_have_zero_rows_and_no_fabricated_scores(self):
        for table in ["users", "movies", "ratings"]:
            self.write(table, "")
        result = self.profile()
        self.assertEqual(result["tables"]["ratings"]["time_distribution"]["quantiles"], {})
        self.assertTrue(all(table["rows"] == 0 for table in result["manifest"]["tables"]))
        self.assertNotIn("scores", result)

    def test_version_depends_on_file_content_not_profile_time(self):
        first = self.profile()["manifest"]["ref"]
        second = profile_dataset(self.source, as_of=datetime(2027, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(first, second["manifest"]["ref"])
        self.write("ratings", "1::1::4::946684800\n")
        self.assertNotEqual(first, self.profile()["manifest"]["ref"])

    def test_missing_file_and_mid_scan_mutation_are_rejected(self):
        def mutate(message):
            if message == "Scanning movies.dat":
                with (self.source / "users.dat").open("ab") as handle:
                    handle.write(b"3::M::25::7::12345\n")
        with self.assertRaisesRegex(RuntimeError, "Input changed"):
            profile_dataset(self.source, progress=mutate, as_of=AS_OF)
        (self.source / "ratings.dat").unlink()
        with self.assertRaisesRegex(FileNotFoundError, "ratings.dat"):
            self.profile()


if __name__ == "__main__":
    unittest.main()
