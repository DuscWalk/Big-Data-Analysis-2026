"""Read-only exploration of MovieLens files; never a substitute for Hadoop scoring."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Callable

from .. import __version__
from ..contracts import ArtifactRef, DatasetManifest, TableManifest

SCHEMAS = {
    "users": ["UserID", "Gender", "Age", "Occupation", "Zip-code"],
    "movies": ["MovieID", "Title", "Genres"],
    "ratings": ["UserID", "MovieID", "Rating", "Timestamp"],
}
AGES = {1, 18, 25, 35, 45, 50, 56}
GENRES = {
    "Action", "Adventure", "Animation", "Children's", "Comedy", "Crime",
    "Documentary", "Drama", "Fantasy", "Film-Noir", "Horror", "Musical",
    "Mystery", "Romance", "Sci-Fi", "Thriller", "War", "Western",
}
INTEGER = re.compile(r"[+-]?[0-9]+\Z")
ZIP_PATTERN = re.compile(r"[0-9]{5}(?:-[0-9]{4})?\Z")
MAX_TIMESTAMP = 253402300799  # Last whole second representable in UTC year 9999.


def _integer(value: str) -> int | None:
    if len(value) > 20 or not INTEGER.fullmatch(value):
        return None
    return int(value)


def _identifier(value: str) -> int | None:
    number = _integer(value)
    return number if number is not None and 0 < number <= 2**63 - 1 else None


def _digest(value) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@dataclass(slots=True)
class _Group:
    signature: bytes
    first_line: int
    first_offset: int
    rows: int = 1
    conflict: bool = False


def _scan(path: Path, table: str, sample_limit: int, now: int,
          parents: dict, progress: Callable[[str], None] | None):
    if progress:
        progress(f"Scanning {path.name}")
    initial = _stamp(path.stat())
    checksum = hashlib.sha256()
    raw_signatures = set()
    groups: dict[tuple, _Group] = {}
    findings = Counter()
    samples = defaultdict(list)
    field_counts = Counter()
    times = []
    rows = parsed_rows = offset = 0
    min_id = max_id = None

    def sample(code, line, start, raw, **details):
        if len(samples[code]) < sample_limit:
            samples[code].append({"file_name": path.name, "line": line,
                                  "byte_offset": start, "raw_preview": raw[:240],
                                  "preview_truncated": len(raw) > 240, **details})

    with path.open("rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != initial:
            raise RuntimeError(f"Input changed before opening: {path.name}")
        for rows, raw_line in enumerate(stream, 1):
            start = offset
            offset += len(raw_line)
            checksum.update(raw_line)
            payload = raw_line.removesuffix(b"\n").removesuffix(b"\r")
            raw = payload.decode("iso-8859-1")
            flags = set()
            fingerprint = hashlib.sha256(payload).digest()
            if fingerprint in raw_signatures:
                flags.add("exact_duplicate_extra_row")
            raw_signatures.add(fingerprint)
            fields = raw.split("::")
            field_counts[str(len(fields))] += 1
            key = signature = None
            if not raw.strip():
                flags.add("blank_row")
            elif len(fields) != len(SCHEMAS[table]):
                flags.add("wrong_field_count")
            else:
                parsed_rows += 1
                values = [field.strip() for field in fields]
                if values != fields:
                    flags.add("surrounding_whitespace")
                if any(not value for value in values):
                    flags.add("missing_field")
                entity_id = _identifier(values[0])
                if entity_id is None:
                    flags.add("invalid_primary_id")
                else:
                    min_id = entity_id if min_id is None else min(min_id, entity_id)
                    max_id = entity_id if max_id is None else max(max_id, entity_id)
                if table == "users":
                    _, gender, age_text, occupation_text, zip_code = values
                    age, occupation = _integer(age_text), _integer(occupation_text)
                    if gender not in {"F", "M"}:
                        flags.add("invalid_gender")
                    if age not in AGES:
                        flags.add("invalid_age_code")
                    if occupation is None or not 0 <= occupation <= 20:
                        flags.add("invalid_occupation_code")
                    if zip_code and not ZIP_PATTERN.fullmatch(zip_code):
                        flags.add("unusual_zip_format")
                    if entity_id is not None:
                        key = (entity_id,)
                        signature = (gender, age if age is not None else age_text,
                                     occupation if occupation is not None else occupation_text, zip_code)
                elif table == "movies":
                    _, title, genre_text = values
                    genres = genre_text.split("|") if genre_text else []
                    if not title:
                        flags.add("missing_title")
                    elif not re.search(r"\([0-9]{4}\)$", title):
                        flags.add("title_without_terminal_year")
                    if not genres or any(genre not in GENRES for genre in genres):
                        flags.add("invalid_genres")
                    if len(genres) != len(set(genres)):
                        flags.add("duplicate_genre_label")
                    if not payload.isascii():
                        try:
                            payload.decode("utf-8")
                        except UnicodeDecodeError:
                            pass
                        else:
                            flags.add("nonascii_row_also_decodes_as_utf8")
                    if entity_id is not None:
                        key = (entity_id,)
                        signature = (title, sorted(set(genres)))
                else:
                    _, movie_text, rating_text, time_text = values
                    movie_id = _identifier(movie_text)
                    rating, timestamp = _integer(rating_text), _integer(time_text)
                    if movie_id is None:
                        flags.add("invalid_movie_id")
                    if rating is None or not 1 <= rating <= 5:
                        flags.add("invalid_rating")
                    valid_time = timestamp is not None and 0 <= timestamp <= MAX_TIMESTAMP
                    if not valid_time:
                        flags.add("invalid_timestamp")
                    else:
                        times.append(timestamp)
                        if timestamp > now:
                            flags.add("timestamp_after_profile_time")
                    for parent, identifier in (("users", entity_id), ("movies", movie_id)):
                        if identifier is not None:
                            group = parents[parent].get((identifier,))
                            if group is None:
                                flags.add(f"reference_missing_from_parseable_{parent}")
                            elif group.conflict:
                                flags.add(f"reference_to_conflicting_{parent}")
                    if entity_id is not None and movie_id is not None and valid_time:
                        key = (entity_id, movie_id, timestamp)
                        signature = rating if rating is not None else rating_text
                if key is not None:
                    business_digest = bytes.fromhex(_digest(signature))
                    group = groups.get(key)
                    if group is None:
                        groups[key] = _Group(business_digest, rows, start)
                    else:
                        group.rows += 1
                        if group.signature != business_digest:
                            group.conflict = True
                            sample("conflicting_business_key", rows, start, raw,
                                   key=list(key), first_line=group.first_line,
                                   first_byte_offset=group.first_offset)
            for code in flags:
                findings[code] += 1
                sample(code, rows, start, raw)
        if _stamp(os.fstat(stream.fileno())) != initial:
            raise RuntimeError(f"Input changed while scanning: {path.name}")
    if _stamp(path.stat()) != initial or offset != initial[2]:
        raise RuntimeError(f"Input changed during scan: {path.name}")

    grouped_rows = sum(group.rows for group in groups.values())
    result = {
        "rows": rows, "parsed_rows": parsed_rows,
        "unparsed_rows": rows - parsed_rows,
        "field_count_distribution": dict(sorted(field_counts.items())),
        "findings": dict(sorted(findings.items())),
        "samples": dict(sorted(samples.items())),
        "id_range": {"minimum": min_id, "maximum": max_id},
        "business_keys": {
            "distinct": len(groups), "rows_without_valid_key": rows - grouped_rows,
            "repeated_key_groups": sum(group.rows > 1 for group in groups.values()),
            "repeated_key_extra_rows": grouped_rows - len(groups),
            "conflicting_key_groups": sum(group.conflict for group in groups.values()),
            "rows_in_conflicting_groups": sum(group.rows for group in groups.values() if group.conflict),
        },
    }
    if table == "ratings":
        times.sort()
        distribution = Counter(datetime.fromtimestamp(t, timezone.utc).year for t in times)
        quantiles = {}
        if times:
            for label, fraction in (("min", 0), ("p25", .25), ("p50", .5),
                                    ("p75", .75), ("p95", .95), ("max", 1)):
                timestamp = times[int((len(times) - 1) * fraction)]
                quantiles[label] = {"unix_seconds": timestamp,
                                    "utc": datetime.fromtimestamp(timestamp, timezone.utc).isoformat()}
        result["time_distribution"] = {
            "scope": "parseable timestamp in [0, 253402300799], independent of other field validity",
            "count": len(times), "by_year_utc": dict(sorted(distribution.items())),
            "quantiles": quantiles,
        }
    manifest = TableManifest(name=table, file_name=path.name, sha256=checksum.hexdigest(),
                             size_bytes=offset, rows=rows, fields=SCHEMAS[table])
    return result, manifest, groups, initial


def profile_dataset(data_dir: Path, *, sample_limit: int = 3,
                    progress: Callable[[str], None] | None = None,
                    as_of: datetime | None = None) -> dict:
    """Read all three source files, returning an audit and exact-content manifest."""
    if not 0 <= sample_limit <= 20:
        raise ValueError("sample_limit must be between 0 and 20")
    source = Path(data_dir).resolve(strict=True)
    for table in SCHEMAS:
        if not (source / f"{table}.dat").is_file():
            raise FileNotFoundError(f"Missing source file: {table}.dat")
    moment = as_of or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("as_of must contain an explicit timezone")
    moment = moment.astimezone(timezone.utc)
    parents, tables, manifests, stamps = {}, {}, [], {}
    for table in SCHEMAS:
        path = source / f"{table}.dat"
        result, manifest, groups, initial = _scan(
            path, table, sample_limit, int(moment.timestamp()), parents, progress)
        tables[table] = result
        manifests.append(manifest)
        stamps[path] = initial
        if table != "ratings":
            parents[table] = groups
        del groups
    for path, initial in stamps.items():
        if _stamp(path.stat()) != initial:
            raise RuntimeError(f"Input changed before profile completed: {path.name}")
    version = "sha256-" + _digest({table.file_name: table.sha256 for table in manifests})
    manifest = DatasetManifest(ref=ArtifactRef(artifact_id="ml-1m.raw", version=version),
                               source_directory=str(source), tables=manifests)
    return {
        "schema_version": "1", "profile_kind": "local_read_only_exploration",
        "profiled_at": moment.isoformat(), "profiler_version": __version__,
        "profiler_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "manifest": manifest.model_dump(mode="json"), "tables": tables,
        "notes": [
            "This profile does not clean data, compute five-dimensional scores or replace Hadoop evaluation.",
            "Finding counts overlap; postal/title/encoding/time observations are not deletion decisions.",
            "Exact duplicate rows ignore line endings; business keys normalize whitespace and integer spelling.",
            "References use primary keys from correctly shaped parent rows, including conflicting or invalid attributes.",
            "A conflict sample is the later differing row; conflict group row totals include all associated rows.",
            "Timestamp quantiles include suspicious but representable events; no T1/T2 or historical cutoff is selected.",
        ],
    }
