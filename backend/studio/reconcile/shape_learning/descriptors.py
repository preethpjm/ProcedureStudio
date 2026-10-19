"""
Extracts interpretable shape descriptors from a converted .glb file, per
named mesh. Validated against a real converted GLB (F6137-D24907000.glb)
before being written here — see the specific numbers that motivated each
descriptor in the comments below.

Deliberately classical/interpretable descriptors, not a learned embedding —
every value here can be explained in one sentence, matching the rest of
this reconciliation pipeline's design philosophy (never a black box).
"""

import json
import struct
import numpy as np
import trimesh


def _read_gltf_json(glb_path: str) -> dict:
    with open(glb_path, "rb") as f:
        f.read(12)  # magic, version, length header — not needed here
        chunk_len, _ = struct.unpack("<II", f.read(8))
        return json.loads(f.read(chunk_len))


def _build_mesh_identity_map(gltf_json: dict) -> dict:
    """Maps glTF mesh index -> best available identity string.

    Confirmed real case: not every GLB has the partId/partNumber extras
    (older files predate that feature) — falls back to the plain node
    name, then a generic placeholder, so this never crashes on an older
    file, it just gets a less precise identity for it.
    """
    identity_map = {}
    for node in gltf_json.get("nodes", []):
        if "mesh" not in node:
            continue
        extras = node.get("extras") or {}
        identity = extras.get("partNumber") or node.get("name") or f"mesh_{node['mesh']}"
        identity_map[node["mesh"]] = identity
    return identity_map


def compute_descriptors(mesh: "trimesh.Trimesh") -> dict:
    """Returns None if the mesh is too degenerate to analyze (confirmed
    real case: at least one mesh in a real file had no usable vertex
    bounds at all — likely a symbolic/reference geometry marker, not a
    real physical part). Callers should skip entries where this is None,
    not treat it as an error.
    """
    v = mesh.vertices
    if v is None or len(v) < 4:
        return None

    try:
        extents = mesh.bounding_box.extents
    except Exception:
        return None
    if extents is None or np.any(np.isnan(extents)) or np.all(extents == 0):
        return None

    sorted_extents = sorted(extents, reverse=True)
    aspect_ratio = sorted_extents[0] / max(sorted_extents[2], 1e-9)

    try:
        hull_volume = mesh.convex_hull.volume
        raw_volume = abs(mesh.volume)
        convexity = raw_volume / hull_volume if hull_volume > 0 else 0.0
    except Exception:
        convexity = 0.0

    centered = v - v.mean(axis=0)
    cov = np.cov(centered.T)
    try:
        eigvals = np.sort(np.linalg.eigvalsh(cov))[::-1]
    except Exception:
        eigvals = np.array([1.0, 1.0, 1.0])
    total = eigvals.sum()
    if total <= 0 or eigvals[0] <= 0:
        linearity, planarity = 0.0, 0.0
    else:
        linearity = float((eigvals[0] - eigvals[1]) / eigvals[0])
        planarity = float((eigvals[1] - eigvals[2]) / eigvals[0])

    return {
        "dims": [round(float(x), 5) for x in sorted_extents],  # largest-to-smallest, so rotation doesn't affect comparison
        "aspect_ratio": round(float(aspect_ratio), 4),
        "convexity": round(float(convexity), 4),
        "linearity": round(linearity, 4),
        "planarity": round(planarity, 4),
    }


def to_vector(descriptors: dict) -> np.ndarray:
    """Flat numeric vector for nearest-neighbor distance — dims included
    as absolute values (not just scale-invariant ratios), since real part
    SIZE is a genuinely strong discriminator in this domain (an M4 screw
    and an M8 screw share a shape profile but not a size).
    """
    d = descriptors
    return np.array([*d["dims"], d["aspect_ratio"], d["convexity"], d["linearity"], d["planarity"]])


def extract_all_descriptors(glb_path: str) -> list:
    """Returns a list of {identity, descriptors} for every analyzable mesh
    in a converted GLB. Skips (doesn't crash on) degenerate meshes.
    """
    gltf_json = _read_gltf_json(glb_path)
    identity_map = _build_mesh_identity_map(gltf_json)

    scene = trimesh.load(glb_path)
    results = []
    for i, (_, mesh) in enumerate(scene.geometry.items()):
        identity = identity_map.get(i, f"mesh_{i}")
        desc = compute_descriptors(mesh)
        if desc is None:
            continue
        results.append({"identity": identity, "descriptors": desc})
    return results

