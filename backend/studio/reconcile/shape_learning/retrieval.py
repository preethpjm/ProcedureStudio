import numpy as np
from .descriptors import to_vector
from .corpus import get_all_entries


def find_nearest(query_descriptors: dict, k: int = 3, exclude_part_number: str = None):
    """Returns up to k closest corpus entries to query_descriptors, each as
    {part_number, description, distance}, sorted nearest-first. Plain
    Euclidean distance over the descriptor vector — genuinely fast enough
    at this corpus scale (milliseconds even at tens of thousands of
    entries), no vector index needed.

    Descriptor dimensions have very different natural scales (dims in
    meters, ratios in 0-1 or unbounded) — normalizing by each dimension's
    spread across the CURRENT corpus keeps any one dimension (e.g. a large
    absolute size) from silently dominating the distance.
    """
    entries = get_all_entries()
    if exclude_part_number:
        entries = [e for e in entries if e["part_number"] != exclude_part_number]
    if not entries:
        return []

    vectors = np.array([to_vector(e["descriptors"]) for e in entries])
    query_vec = to_vector(query_descriptors)

    spread = vectors.std(axis=0)
    spread[spread == 0] = 1.0  # avoid divide-by-zero for a dimension with no variance yet

    normalized_vectors = vectors / spread
    normalized_query = query_vec / spread

    distances = np.linalg.norm(normalized_vectors - normalized_query, axis=1)
    order = np.argsort(distances)[:k]

    return [
        {
            "part_number": entries[i]["part_number"],
            "description": entries[i]["description"],
            "distance": round(float(distances[i]), 4),
        }
        for i in order
    ]

