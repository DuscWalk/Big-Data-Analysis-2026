"""Read one exact cleaned version and one temporal partition, without Hadoop."""
import argparse
import json

from movielens_agent.contracts import ArtifactRef
from movielens_agent.storage.cleaned import CleanedDataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--split", required=True, choices=("train", "validation", "test"))
    args = parser.parse_args()
    dataset = CleanedDataset(args.manifest, expected_task_id=args.task_id,
                             expected_ref=ArtifactRef(artifact_id=args.artifact_id, version=args.version))
    dataset.verify()
    examples, count = [], 0
    # Stream all selected rows so count checks finish. Do not accumulate the
    # complete ratings table or fit transformations on validation/test rows.
    for count, row in enumerate(dataset.ratings(args.split), 1):
        if len(examples) < 3:
            examples.append(row)
    print(json.dumps({"ref": dataset.manifest["ref"], "producer_task_id": args.task_id,
                      "split": args.split, "rows": count, "examples": examples,
                      "verification": "all file hashes and selected partition count passed"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
