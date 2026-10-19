"""Converter adapters. Both produce the same outputs (GLB + product_model.json + meshes.npz).

* ``occ``  — Open CASCADE in-process (``occ_converter``): exact B-rep analysis, cylinder
  axes, real STEP PRODUCT ids. Default; works on Windows, Linux and in containers.
* ``exe``  — the existing ``cad-step-to-glb.exe`` (Windows). Its GLB and BOM JSON are turned
  into the same product model from the GLB scene graph (trimesh): sizes, volumes and
  positions, without B-rep cylinder axes (the sequence planner then uses the model axes only).
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from .occ_converter import ConversionError, convert_step, safe_pn

log = logging.getLogger("studio.cad")


class Converter:
    kind = "base"

    def available(self) -> tuple[bool, str]:
        return False, "not implemented"

    def convert(self, step: Path, out: Path, on_progress=None) -> dict:
        raise NotImplementedError


class OccConverter(Converter):
    kind = "occ"

    def __init__(self, quality: str = "balanced"):
        self.quality = quality

    def available(self):
        try:
            import OCP  # noqa: F401
            return True, "Open CASCADE (OCP) in-process"
        except ImportError:
            return False, "OCP not installed: pip install cadquery-ocp==7.8.1.1.post1"

    def convert(self, step, out, on_progress=None):
        return convert_step(step, out, self.quality, on_progress)


class ExeConverter(Converter):
    kind = "exe"

    def __init__(self, exe_path: str, quality: str = "balanced", mode: str = "auto", timeout: int = 5600):
        self.exe, self.quality, self.mode, self.timeout = Path(exe_path), quality, mode, timeout

    def available(self):
        return (self.exe.exists(), str(self.exe) if self.exe.exists() else f"{self.exe} not found")

    def convert(self, step, out, on_progress=None):
        out.mkdir(parents=True, exist_ok=True)
        glb, bom = out / f"{step.stem}.glb", out / f"{step.stem}_bom.json"
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        p = subprocess.run([str(self.exe), str(step), str(glb), str(bom), self.mode, self.quality, "500"],
                           capture_output=True, text=True, timeout=self.timeout, creationflags=flags)
        if p.returncode != 0 or not glb.exists():
            raise ConversionError(f"cad-step-to-glb failed ({p.returncode}): {p.stderr[-800:] or p.stdout[-800:]}")
        legacy = json.loads(bom.read_text()) if bom.exists() else {}
        return product_model_from_glb(glb, legacy, out, step.name)


def _mm(T) -> list[float]:
    M = np.array(T, dtype=float).copy()
    M[:3, 3] *= 1000.0
    return [round(float(v), 6) for v in M.ravel()]


def product_model_from_glb(glb: Path, legacy: dict, out: Path, source_name: str) -> dict:
    """Product model from any GLB (node tree + meshes). Units: GLB metres -> mm."""
    import trimesh
    t0 = time.monotonic()
    scene = trimesh.load(str(glb), force="scene")
    graph = scene.graph
    occs, meshes, counter = [], {}, {}

    def pn_of(name: str) -> str:
        return name.rsplit("_", 1)[0] if name.rsplit("_", 1)[-1].isdigit() else name

    by_node = {}
    for node in graph.nodes:
        if node == graph.base_frame:
            continue
        parent = graph.transforms.parents.get(node)
        name = str(node)
        pn = pn_of(name)
        counter[safe_pn(pn)] = counter.get(safe_pn(pn), 0) + 1
        occ_id = name if name.rsplit("_", 1)[-1].isdigit() else f"{safe_pn(pn)}_{counter[safe_pn(pn)]}"
        T, geom = graph.get(node)
        o = {"occ_id": occ_id, "parent": None, "_parent_node": parent, "path": name, "instance_name": name,
             "part_number": pn, "name": pn, "description": "", "is_assembly": geom is None, "depth": 0,
             "world_transform": _mm(T),
             "children": []}
        if geom is not None:
            m = scene.geometry[geom].copy()
            m.apply_transform(T)
            m.apply_scale(1000.0)
            b = m.bounds
            o["geometry"] = {"bbox": [round(float(v), 4) for v in (*b[0], *b[1])],
                             "volume_mm3": round(abs(float(m.volume)), 3) if m.is_volume else None,
                             "area_mm2": round(float(m.area), 3), "face_types": {}, "cylinders": [],
                             "centroid": [round(float(v), 4) for v in m.centroid]}
            meshes[occ_id] = (m.vertices.astype(np.float32), m.faces.astype(np.int32))
        by_node[node] = o
        occs.append(o)
    for o in occs:
        p = by_node.get(o.pop("_parent_node"))
        if p:
            o["parent"] = p["occ_id"]
            p["children"].append(o["occ_id"])
            p["is_assembly"] = True
    roots = [o for o in occs if not o["parent"]]
    if len(roots) != 1:
        root = {"occ_id": "ROOT_1", "parent": None, "path": "ROOT", "instance_name": "", "part_number": glb.stem,
                "name": glb.stem, "description": "", "is_assembly": True, "depth": 0,
                "world_transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], "children": [r["occ_id"] for r in roots]}
        for r in roots:
            r["parent"] = "ROOT_1"
        occs.insert(0, root)
    # names from the legacy BOM when available
    names = {e.get("partNumber"): e for e in (legacy.get("bom") or [])}
    for o in occs:
        e = names.get(o["part_number"])
        if e:
            o["name"] = e.get("name") or o["name"]
    counts = {}
    for o in occs:
        if not o["is_assembly"]:
            c = counts.setdefault(o["part_number"], {"partNumber": o["part_number"], "name": o["name"], "description": "",
                                                     "instances": 0, "material": None})
            c["instances"] += 1
    by_id = {o["occ_id"]: o for o in occs}

    def depth(o):
        d, p = 0, o.get("parent")
        while p:
            d, p = d + 1, by_id[p].get("parent")
        return d
    for o in occs:
        o["depth"] = depth(o)

    def tree(i):
        o = by_id[i]
        return {"name": o["name"], "partNumber": o["part_number"], "isPart": not o["is_assembly"], "occ_id": i,
                "children": [tree(c) for c in o["children"]]}
    root_id = next(o["occ_id"] for o in occs if not o["parent"])
    model = {"format": "studio.product-model/1", "source": {"file": source_name, "converter": "exe+glb"},
             "units": "mm", "up_axis": "Z", "gltf": {"file": glb.name, "units": "m"}, "root": root_id, "quality": None,
             "occurrences": occs, "bom": legacy.get("bom") or sorted(counts.values(), key=lambda e: e["partNumber"]),
             "assembly_tree": legacy.get("assembly_tree") or tree(root_id),
             "stats": {"occurrences": len(occs), "parts": sum(1 for o in occs if not o["is_assembly"]),
                       "products": len({o["part_number"] for o in occs}), "seconds": round(time.monotonic() - t0, 2),
                       "note": "built from the GLB: no B-rep cylinder axes"}}
    (out / "product_model.json").write_text(json.dumps(model, indent=1), encoding="utf-8")
    np.savez_compressed(out / "meshes.npz", **{f"{k}__v": v for k, (v, _) in meshes.items()},
                        **{f"{k}__f": f for k, (_, f) in meshes.items()})
    return {"glb": str(glb), "product_model": str(out / "product_model.json"), "meshes": str(out / "meshes.npz"),
            "stats": model["stats"]}


def get_converter(cfg: dict) -> Converter:
    kind = (cfg or {}).get("kind", "occ")
    if kind == "exe":
        return ExeConverter(cfg.get("exe_path", "engine/cad-step-to-glb.exe"), cfg.get("quality", "balanced"))
    return OccConverter(cfg.get("quality", "balanced"))
