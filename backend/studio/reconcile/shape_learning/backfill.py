"""
One-time (and re-runnable) script: mines PAST reconciliation results plus
their corresponding .glb files for confirmed matches, and populates the
shape corpus. Run manually:

    python -m shape_learning.backfill path/to/reconciliation_result.json path/to/model.glb
"""

import sys
import json
from .descriptors import extract_all_descriptors
from .corpus import add_entry, corpus_size


def backfill_from_result(result_path: str, glb_path: str):
    result = json.load(open(result_path))
    all_descriptors = extract_all_descriptors(glb_path)
    by_identity = {d["identity"]: d["descriptors"] for d in all_descriptors}

    added = 0
    for m in result.get("matched", []):
        if m["status"] not in ("matched", "manual"):
            continue  # only trust exact/manually-confirmed matches as training data
        identity = m["step_part_number"]
        descriptors = by_identity.get(identity)
        if descriptors is None:
            continue
        add_entry(
            part_number=m["mbom_part_number"],
            description=m.get("mbom_description", ""),
            descriptors=descriptors,
            source_file=glb_path,
            confidence_source=m["status"],
        )
        added += 1

    print(f"Added {added} entries. Corpus size now: {corpus_size()}")


if __name__ == "__main__":
    backfill_from_result(sys.argv[1], sys.argv[2])

