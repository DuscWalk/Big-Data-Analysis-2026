"""Self-contained Hadoop Streaming programs (stdlib only).

All business validation, cleaning, grouping, disposition counting and scores run
here inside MapReduce containers. The application only packages/exports records.
"""
import argparse
import base64
from collections import Counter, defaultdict
from itertools import groupby
import json
from pathlib import Path
import re
import sys

FIELDS = {
    "users": ("user_id", "gender", "age_code", "occupation_code", "zip_code"),
    "movies": ("movie_id", "title", "genres"),
    "ratings": ("user_id", "movie_id", "rating", "timestamp"),
}
AGES = {1, 18, 25, 35, 45, 50, 56}
GENRES = {"Action", "Adventure", "Animation", "Children's", "Comedy", "Crime",
          "Documentary", "Drama", "Fantasy", "Film-Noir", "Horror", "Musical",
          "Mystery", "Romance", "Sci-Fi", "Thriller", "War", "Western"}
DIMENSIONS = ("Accurate", "Complete", "Unique", "Up-to-date", "Consistent")
INTEGER = re.compile(r"[+-]?[0-9]+\Z")


def encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def emit(value):
    print(encode(value))


def integer(value):
    if len(value) > 20 or not INTEGER.fullmatch(value):
        return None
    return int(value)


def identifier(value):
    number = integer(value)
    return number if number is not None and 0 < number <= 2**63 - 1 else None


def evaluate(envelope, config):
    table = envelope["table"]
    names = FIELDS[table]
    clean_input = envelope.get("kind") == "record"
    if clean_input:
        record = envelope["data"]
        fields = [("|".join(record[name]) if name == "genres" else str(record[name]))
                  for name in names]
        raw_fields = fields
    else:
        raw = base64.b64decode(envelope["raw_b64"], validate=True).rstrip(b"\r\n")
        raw_fields = raw.decode("iso-8859-1").split("::")
        fields = [part.strip() for part in raw_fields]
    errors, warnings, changes = [], [], []
    checks = 3 if table == "users" else 1
    counts = Counter(rows=1, accurate_population=checks,
                     complete_population=len(names), unique_population=1,
                     consistent_population=1)
    result = {"envelope": envelope, "data": None, "key": None,
              "signature": None, "errors": errors, "warnings": warnings,
              "changes": changes, "counts": counts}
    if len(fields) != len(names):
        errors.append("R01_FIELD_COUNT")
        return result
    counts["complete_passed"] = sum(bool(value) for value in fields)
    if not all(fields):
        errors.append("R02_MISSING_REQUIRED")
    for name, before, after in zip(names, raw_fields, fields):
        if before != after:
            changes.append({"rule": "R03_TRIM", "field": name, "before": before, "after": after})
    primary = identifier(fields[0])
    if primary is None:
        errors.append("R04_IDENTIFIER")
    if table == "users":
        gender, age, occupation, zipcode = fields[1], integer(fields[2]), integer(fields[3]), fields[4]
        valid = [gender in {"M", "F"}, age in AGES,
                 occupation is not None and 0 <= occupation <= 20]
        counts["accurate_passed"] = sum(valid)
        if not all(valid):
            errors.append("R05_USER_DOMAIN")
        if zipcode and not re.fullmatch(r"[0-9]{5}(?:-[0-9]{4})?", zipcode):
            warnings.append("UNVERIFIED_ZIP_FORMAT")
        data = dict(zip(names, (primary, gender, age, occupation, zipcode)))
        key = [primary] if primary is not None else None
        numeric_fields = (0, 2, 3)
    elif table == "movies":
        genres = sorted(set(part.strip() for part in fields[2].split("|")))
        valid = bool(fields[2]) and all(genre in GENRES for genre in genres)
        counts["accurate_passed"] = int(valid)
        if not valid:
            errors.append("R06_MOVIE_GENRE")
        if "|".join(genres) != fields[2]:
            changes.append({"rule": "R07_GENRE_SET", "field": "genres",
                            "before": fields[2], "after": genres})
        if not re.search(r"\([0-9]{4}\)\Z", fields[1]):
            warnings.append("UNVERIFIED_TITLE_YEAR")
        # Evidence of ambiguous encoding, never a guessed automatic conversion.
        if not clean_input and any(ord(c) > 127 for c in fields[1]):
            try:
                alternate = fields[1].encode("iso-8859-1").decode("utf-8")
                if alternate != fields[1]:
                    warnings.append("AMBIGUOUS_TEXT_ENCODING")
            except UnicodeDecodeError:
                pass
        data = dict(zip(names, (primary, fields[1], genres)))
        key = [primary] if primary is not None else None
        numeric_fields = (0,)
    else:
        movie, rating, timestamp = identifier(fields[1]), integer(fields[2]), integer(fields[3])
        counts["accurate_passed"] = int(rating is not None and 1 <= rating <= 5)
        if not counts["accurate_passed"]:
            errors.append("R08_RATING_DOMAIN")
        if movie is None:
            errors.append("R04_MOVIE_IDENTIFIER")
        valid_timestamp = timestamp is not None and 0 <= timestamp <= 253402300799
        if not valid_timestamp:
            errors.append("R09_TIMESTAMP_TYPE")
        elif not config["rules"]["timestamp_min"] <= timestamp <= config["rules"]["timestamp_max"]:
            errors.append("R10_HISTORICAL_RANGE")
        reference = config["metrics"]["reference_time"]
        counts["uptodate_passed"] = int(valid_timestamp and
            reference - config["metrics"]["window_seconds"] <= timestamp <= reference)
        data = dict(zip(names, (primary, movie, rating, timestamp)))
        key = [primary, movie, timestamp] if primary is not None and movie is not None and valid_timestamp else None
        numeric_fields = (0, 1, 2, 3)
    for i in numeric_fields:
        number = integer(fields[i])
        if number is not None and fields[i] != str(number):
            changes.append({"rule": "R11_INTEGER_FORMAT", "field": names[i],
                            "before": fields[i], "after": number})
    # For invalid fields keep their literal normalized values in the signature:
    # two different unparseable values must not collapse into the same None.
    signature = []
    for i, name in enumerate(names):
        value = data[name]
        signature.append(["invalid", fields[i]] if value is None else value)
    result.update(data=data, key=key, signature=encode(signature))
    return result


def attach_references(row, parents, raw_parents=None, cleaning=False):
    if row["envelope"]["table"] != "ratings" or row["data"] is None:
        return
    for table, field in (("users", "user_id"), ("movies", "movie_id")):
        value = row["data"][field]
        if value is None:
            continue
        status = parents.get(table, {}).get(str(value))
        if status is None:
            raw_exists = raw_parents and str(value) in raw_parents.get(table, {})
            label = "PARENT_REMOVED" if cleaning and raw_exists else "PARENT_MISSING"
            row["errors"].append(f"R12_{label}_{table.upper()}")
        elif status["conflict"]:
            row["errors"].append(f"R13_PARENT_CONFLICT_{table.upper()}")


def map_rows(lines, config, parents, raw_parents, mode):
    for line in lines:
        envelope = json.loads(line)
        if envelope.get("kind", "raw") not in ("raw", "record"):
            continue
        row = evaluate(envelope, config)
        attach_references(row, parents, raw_parents, cleaning=mode == "clean")
        # Invalid keys are never grouped as one fictitious business entity.
        key = row["key"]
        if key is None:
            key = ["invalid", envelope["source_ref"]["file_name"],
                   envelope["source_ref"]["byte_offset"]]
        group_key = encode([envelope["table"], key])
        print(group_key + "\t" + encode(row))


def source_order(row):
    ref = row["envelope"]["source_ref"]
    return ref["file_name"], ref["byte_offset"]


def reduce_group(rows, mode, config):
    """Bound memory for unusually large repeated-key groups with a disk spool."""
    import tempfile
    totals = Counter()
    first_signature = valid_signature = None
    conflict = valid_conflict = False
    representative = None
    table = key = None
    with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+t") as spool:
        for row in rows:
            table = row["envelope"]["table"]
            key = row["key"]
            totals.update(row["counts"])
            if key is not None:
                if first_signature is None:
                    first_signature = row["signature"]
                conflict |= first_signature != row["signature"]
            if not row["errors"]:
                if valid_signature is None:
                    valid_signature = row["signature"]
                valid_conflict |= valid_signature != row["signature"]
                if representative is None or source_order(row) < source_order(representative):
                    representative = row
            spool.write(encode(row) + "\n")
        if mode == "score":
            totals["unique_passed"] = int(key is not None)
            totals["conflict_groups"] = int(conflict)
            totals["conflict_rows"] = totals["rows"] if conflict else 0
            totals["repeated_key_extra_rows"] = totals["rows"] - 1 if key is not None else 0
        spool.seek(0)
        for line in spool:
            row = json.loads(line)
            if mode == "score":
                totals["consistent_passed"] += int(not row["errors"] and not conflict)
                continue
            reasons = list(row["errors"])
            retained = None
            if reasons:
                disposition = "quarantined"
            elif valid_conflict:
                disposition = "quarantined"
                reasons.append("R14_VALID_KEY_CONFLICT")
            elif source_order(row) != source_order(representative):
                disposition = "deduplicated"
                reasons.append("R15_DUPLICATE")
                retained = representative["envelope"]["source_ref"]
            else:
                disposition = "repaired" if row["changes"] else "kept"
                emit({"kind": "record", "table": table, "data": row["data"],
                      "source_ref": row["envelope"]["source_ref"]})
            emit({"kind": "disposition", "table": table,
                  "source_ref": row["envelope"]["source_ref"],
                  "raw_b64": row["envelope"].get("raw_b64"),
                  "disposition": disposition, "reasons": reasons,
                  "warnings": row["warnings"], "changes": row["changes"],
                  "retained_source_ref": retained})
        if mode == "score":
            emit({"kind": "counts", "table": table, "counts": dict(totals)})
        if table in ("users", "movies") and key is not None:
            if mode == "score" or (representative is not None and not valid_conflict):
                emit({"kind": "parent", "table": table, "id": key[0],
                      "conflict": conflict if mode == "score" else False})


def reduce_rows(lines, mode, config):
    def unpack(line):
        key, payload = line.rstrip("\n").split("\t", 1)
        return key, json.loads(payload)
    for _, group in groupby(map(unpack, lines), key=lambda item: item[0]):
        reduce_group((row for _, row in group), mode, config)


def add_sample(samples, key, item, limit=3):
    bucket = samples.setdefault(key, [])
    if any(existing["source_ref"] == item["source_ref"] for existing in bucket):
        return
    bucket.append(item)
    bucket.sort(key=lambda entry: (entry["source_ref"]["file_name"],
                                   entry["source_ref"]["byte_offset"]))
    del bucket[limit:]


def aggregate_map(lines, config):
    stats = {table: Counter() for table in FIELDS}
    dispositions = {table: Counter() for table in FIELDS}
    reasons = {table: Counter() for table in FIELDS}
    warnings = {table: Counter() for table in FIELDS}
    changes = {table: Counter() for table in FIELDS}
    splits = Counter(train=0, validation=0, test=0)
    samples = {}
    for line in lines:
        item = json.loads(line)
        kind, table = item["kind"], item["table"]
        if kind == "counts":
            stats[table].update(item["counts"])
        elif kind == "disposition":
            dispositions[table][item["disposition"]] += 1
            reasons[table].update(item["reasons"])
            warnings[table].update(item["warnings"])
            changes[table].update(change["rule"] for change in item["changes"])
            for reason in item["reasons"] + item["warnings"] + [c["rule"] for c in item["changes"]]:
                add_sample(samples, table + "/" + reason, item)
        elif kind == "record" and table == "ratings":
            stamp = item["data"]["timestamp"]
            split = ("train" if stamp <= config["split"]["train_end"] else
                     "validation" if stamp <= config["split"]["validation_end"] else "test")
            splits[split] += 1
    print("all\t" + encode({"stats": stats, "dispositions": dispositions, "reasons": reasons,
                            "warnings": warnings, "changes": changes, "splits": splits,
                            "samples": samples}))


def ratio(passed, population):
    if population == 0:
        return {"status": "not_evaluable", "score": None, "passed": passed,
                "population": population, "reason": "Empty population."}
    return {"status": "computed", "score": 100 * passed / population,
            "passed": passed, "population": population}


def aggregate_reduce(lines, config):
    sums = {name: {table: Counter() for table in FIELDS}
            for name in ("stats", "dispositions", "reasons", "warnings", "changes")}
    splits = Counter(train=0, validation=0, test=0)
    samples = {}
    for line in lines:
        item = json.loads(line.split("\t", 1)[1])
        for name in sums:
            for table in FIELDS:
                sums[name][table].update(item[name][table])
        splits.update(item["splits"])
        for key, entries in item["samples"].items():
            for entry in entries:
                add_sample(samples, key, entry)
    tables = {}
    for table, counts in sums["stats"].items():
        scores = {}
        for dimension, prefix in zip(DIMENSIONS, ("accurate", "complete", "unique", "uptodate", "consistent")):
            if dimension == "Up-to-date" and table != "ratings":
                scores[dimension] = {"status": "not_applicable", "score": None,
                                     "reason": "No event timestamp in this table."}
            else:
                population = counts["rows"] if prefix == "uptodate" else counts[prefix + "_population"]
                scores[dimension] = ratio(counts[prefix + "_passed"], population)
        tables[table] = {"rows": counts["rows"], "metrics": scores, "counts": dict(counts)}
    overall = {}
    for dimension in DIMENSIONS:
        applicable = ("ratings",) if dimension == "Up-to-date" else tuple(FIELDS)
        values = [tables[table]["metrics"][dimension]["score"] for table in applicable]
        valid = all(score is not None for score in values)
        overall[dimension] = {
            "status": "computed" if valid else "not_evaluable",
            "score": sum(values) / len(values) if valid else None,
            "weights": {table: 1 / len(applicable) for table in applicable},
            "reason": None if valid else "A required table has no evaluable rows.",
        }
    emit({"schema_version": "1", "kind": "quality", "tables": tables, "overall": overall,
          "dispositions": sums["dispositions"], "reasons": sums["reasons"],
          "warnings": sums["warnings"], "changes": sums["changes"],
          "splits": splits, "samples": samples, "configuration": config})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("map", "reduce", "aggregate-map", "aggregate-reduce"))
    parser.add_argument("--mode", choices=("score", "clean"), default="score")
    parser.add_argument("--config", default="configuration.json")
    parser.add_argument("--parents")
    parser.add_argument("--raw-parents")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    parents = json.loads(Path(args.parents).read_text()) if args.parents else {}
    raw_parents = json.loads(Path(args.raw_parents).read_text()) if args.raw_parents else {}
    if args.operation == "map":
        map_rows(sys.stdin, config, parents, raw_parents, args.mode)
    elif args.operation == "reduce":
        reduce_rows(sys.stdin, args.mode, config)
    elif args.operation == "aggregate-map":
        aggregate_map(sys.stdin, config)
    else:
        aggregate_reduce(sys.stdin, config)


if __name__ == "__main__":
    main()
