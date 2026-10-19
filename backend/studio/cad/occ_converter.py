"""STEP -> GLB + product model, using Open CASCADE (OCP bindings).

Cross-platform replacement for the Windows-only ``cad-step-to-glb.exe``. One read of the
STEP file produces:

* ``<name>.glb``           — tessellated assembly, Y-up metres (glTF convention). Every
                             node is named with its ``occ_id`` and carries
                             ``extras = {occ_id, part_number, name, instance, path}``.
* ``product_model.json``   — ``studio.product-model/1``: every occurrence with its real
                             STEP PRODUCT id / name / description, placement, geometry
                             facts (bounding box, volume, area, cylindrical features) in
                             STEP coordinates (millimetres, Z-up as authored).
* ``meshes.npz``           — world-space triangle meshes per part occurrence (mm), used by
                             contact detection and the sequence planner.

The ``bom`` / ``assembly_tree`` sections reproduce the legacy converter's JSON so the
existing reconciliation code runs unchanged.

``occ_id`` is ``<sanitised part number>_<n>`` (n = instance counter per part number in
tree order). It survives three.js name sanitising, matches ASTHRA's GLB part locator
(part number + instance suffix) and is stable across re-exports of the same design.
"""
from __future__ import annotations

import json
import logging
import math
import re
import struct
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

import numpy as np

from .step_text import StepStructure, read_step_structure

log = logging.getLogger("studio.cad")

QUALITY = {  # linear deflection (mm), angular deflection (rad)
    "fast": (0.25, 0.6),
    "balanced": (0.08, 0.35),
    "precise": (0.02, 0.2),
}


class ConversionError(Exception):
    pass


def _ocp():
    """Import OCP lazily so the rest of the app works (with the external converter) without it."""
    try:
        from OCP import (BRep, BRepAdaptor, BRepBndLib, BRepGProp, BRepMesh, Bnd, GeomAbs, GProp,  # noqa: F401
                         IFSelect, Message, STEPCAFControl, TCollection, TColStd, TDataStd, TDF, TDocStd,
                         TopAbs, TopExp, TopLoc, TopoDS, XCAFDoc, RWGltf)
        from OCP import Quantity  # noqa: F401
        import OCP
        return OCP
    except ImportError as e:  # pragma: no cover - environment dependent
        raise ConversionError("Open CASCADE (pip install cadquery-ocp==7.8.1.1.post1) is not installed: " + str(e))


_SAFE = re.compile(r"[^A-Za-z0-9\-]+")


def safe_pn(pn: str) -> str:
    s = _SAFE.sub("-", (pn or "").strip()).strip("-")
    return s or "UNNAMED"


def _label_name(OCP, label) -> str:
    attr = OCP.TDataStd.TDataStd_Name()
    if label.FindAttribute(OCP.TDataStd.TDataStd_Name.GetID_s(), attr):
        return attr.Get().ToExtString()
    return ""


def _set_label_name(OCP, label, name: str) -> None:
    OCP.TDataStd.TDataStd_Name.Set_s(label, OCP.TCollection.TCollection_ExtendedString(name))


# Machined parts are often modelled with their manufacturing phases or bodies as sub-products (raw stock,
# phase 10, 20, finished part, construction bodies). They are one part for the IPL and the procedures.
PHASE_RE = re.compile(r"(?:^|[^A-Z])(PHASE|PH\d|BRUT|STOCK|BLANK|ETAPE|OP\d{2,3}|BODY|BODIES|CORPS|PARTBODY|PSV|USIN|"
                      r"FORGING|CASTING|EBAUCHE|SEMI[- ]?FINI|FINI|FINISHED|MACHINED)(?:[^A-Z]|$)")
FINAL_RE = re.compile(r"(?:^|[^A-Z])(FINI|FINISHED|FINAL|USINE|MACHINED|PIECE|PART)(?:[^A-Z]|$)")


def _iou(a, b) -> float:
    if a is None or b is None:
        return 0.0
    lo = [max(a[i], b[i]) for i in range(3)]
    hi = [min(a[i + 3], b[i + 3]) for i in range(3)]
    inter = 1.0
    for i in range(3):
        inter *= max(0.0, hi[i] - lo[i])
    va = max(1e-9, (a[3] - a[0]) * (a[4] - a[1]) * (a[5] - a[2]))
    vb = max(1e-9, (b[3] - b[0]) * (b[4] - b[1]) * (b[5] - b[2]))
    return inter / (va + vb - inter)


def _trsf_matrix(OCP, loc) -> list[float]:
    t = loc.Transformation()
    m = [[t.Value(r, c) for c in range(1, 5)] for r in range(1, 4)] + [[0.0, 0.0, 0.0, 1.0]]
    return [round(v, 6) for row in m for v in row]   # row-major 4x4


def _bbox(OCP, shape) -> list[float] | None:
    box = OCP.Bnd.Bnd_Box()
    OCP.BRepBndLib.BRepBndLib.Add_s(shape, box, True)
    if box.IsVoid():
        return None
    x0, y0, z0, x1, y1, z1 = box.Get()
    return [round(v, 4) for v in (x0, y0, z0, x1, y1, z1)]


def _props(OCP, shape) -> dict:
    out = {}
    try:
        gp = OCP.GProp.GProp_GProps()
        OCP.BRepGProp.BRepGProp.VolumeProperties_s(shape, gp)
        out["volume_mm3"] = round(abs(gp.Mass()), 3)
        c = gp.CentreOfMass()
        out["centroid"] = [round(c.X(), 4), round(c.Y(), 4), round(c.Z(), 4)]
        sp = OCP.GProp.GProp_GProps()
        OCP.BRepGProp.BRepGProp.SurfaceProperties_s(shape, sp)
        out["area_mm2"] = round(sp.Mass(), 3)
    except Exception:  # degenerate geometry: keep going without mass properties
        pass
    return out


def _faces(OCP, shape):
    exp = OCP.TopExp.TopExp_Explorer(shape, OCP.TopAbs.TopAbs_FACE)
    while exp.More():
        yield OCP.TopoDS.TopoDS.Face_s(exp.Current())
        exp.Next()


def _features(OCP, shape) -> tuple[dict, list[dict]]:
    """Face-type counts and cylindrical features (axis in the shape's own placement)."""
    kinds = Counter()
    cyl: dict[tuple, dict] = {}
    GA = OCP.GeomAbs
    names = {GA.GeomAbs_Plane: "plane", GA.GeomAbs_Cylinder: "cylinder", GA.GeomAbs_Cone: "cone",
             GA.GeomAbs_Sphere: "sphere", GA.GeomAbs_Torus: "torus", GA.GeomAbs_BSplineSurface: "bspline",
             GA.GeomAbs_SurfaceOfRevolution: "revolution"}
    for f in _faces(OCP, shape):
        try:
            s = OCP.BRepAdaptor.BRepAdaptor_Surface(f, True)
        except Exception:
            continue
        t = s.GetType()
        kinds[names.get(t, "other")] += 1
        if t == GA.GeomAbs_Cylinder:
            c = s.Cylinder()
            ax = c.Axis()
            d, p = ax.Direction(), ax.Location()
            dv = np.array([d.X(), d.Y(), d.Z()])
            if dv[np.argmax(np.abs(dv))] < 0:
                dv = -dv
            pv = np.array([p.X(), p.Y(), p.Z()])
            pv = pv - dv * float(np.dot(pv, dv))           # foot point closest to origin: identifies the axis line
            key = (round(c.Radius(), 2), *np.round(dv, 3), *np.round(pv, 1))
            span = (s.FirstVParameter(), s.LastVParameter())
            e = cyl.setdefault(key, {"radius": round(c.Radius(), 4), "dir": [round(float(v), 5) for v in dv],
                                     "point": [round(float(v), 4) for v in pv], "faces": 0, "length": 0.0})
            e["faces"] += 1
            if all(math.isfinite(v) for v in span):
                e["length"] = round(max(e["length"], abs(span[1] - span[0])), 3)
    feats = sorted(cyl.values(), key=lambda e: (-e["faces"], -e["radius"]))[:24]
    return dict(kinds), feats


def _transform_feature(f: dict, m: list[float]) -> dict:
    M = np.array(m).reshape(4, 4)
    R, T = M[:3, :3], M[:3, 3]
    d = R @ np.array(f["dir"])
    p = R @ np.array(f["point"]) + T
    if d[np.argmax(np.abs(d))] < 0:
        d = -d
    return {**f, "dir": [round(float(v), 5) for v in d], "point": [round(float(v), 4) for v in p]}


def _triangles(OCP, shape) -> tuple[np.ndarray, np.ndarray]:
    """World-space triangles of an already meshed (and located) shape."""
    verts, tris, base = [], [], 0
    for f in _faces(OCP, shape):
        loc = OCP.TopLoc.TopLoc_Location()
        tri = OCP.BRep.BRep_Tool.Triangulation_s(f, loc)
        if tri is None:
            continue
        tr = loc.Transformation()
        nb = tri.NbNodes()
        pts = np.empty((nb, 3))
        for i in range(1, nb + 1):
            p = tri.Node(i).Transformed(tr)
            pts[i - 1] = (p.X(), p.Y(), p.Z())
        rev = f.Orientation() == OCP.TopAbs.TopAbs_REVERSED
        idx = np.empty((tri.NbTriangles(), 3), dtype=np.int64)
        for i in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(i).Get()
            idx[i - 1] = (a - 1, c - 1, b - 1) if rev else (a - 1, b - 1, c - 1)
        verts.append(pts)
        tris.append(idx + base)
        base += nb
    if not verts:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    return np.vstack(verts), np.vstack(tris)


def _patch_glb(path: Path, extras_by_name: dict[str, dict], asset_extras: dict) -> int:
    """Add per-node extras (occ_id, part number…) to the GLB JSON chunk. Returns nodes patched."""
    data = path.read_bytes()
    if data[:4] != b"glTF":
        raise ConversionError("glTF writer did not produce a binary GLB")
    clen, _ = struct.unpack_from("<II", data, 12)
    gltf = json.loads(data[20:20 + clen])
    rest = data[20 + clen:]
    n = 0
    for node in gltf.get("nodes", []):
        ex = extras_by_name.get(node.get("name", ""))
        if ex:
            node["extras"] = {**(node.get("extras") or {}), **ex}
            n += 1
    gltf.setdefault("asset", {})["extras"] = asset_extras
    js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    js += b" " * ((4 - len(js) % 4) % 4)
    total = 12 + 8 + len(js) + len(rest)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js + rest)
    return n


def convert_step(step_path: str | Path, out_dir: str | Path, quality: str = "balanced",
                 on_progress: Callable[[str, float], None] | None = None) -> dict:
    """Convert one STEP file. Returns paths of the outputs and a short summary."""
    OCP = _ocp()
    step_path, out_dir = Path(step_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prog = on_progress or (lambda stage, frac: None)
    t0 = time.monotonic()
    lin, ang = QUALITY.get(quality, QUALITY["balanced"])

    prog("Reading the STEP file", 0.02)
    structure: StepStructure = read_step_structure(step_path)

    prog("Reading geometry", 0.05)
    doc = OCP.TDocStd.TDocStd_Document(OCP.TCollection.TCollection_ExtendedString("XmlXCAF"))
    reader = OCP.STEPCAFControl.STEPCAFControl_Reader()
    for setter in ("SetNameMode", "SetColorMode", "SetLayerMode", "SetPropsMode", "SetGDTMode"):
        try:
            getattr(reader, setter)(True)
        except Exception:
            pass
    if reader.ReadFile(str(step_path)) != OCP.IFSelect.IFSelect_RetDone:
        raise ConversionError(f"Open CASCADE could not read {step_path.name} (not a valid STEP file?)")
    reader.Transfer(doc)
    st = OCP.XCAFDoc.XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = OCP.XCAFDoc.XCAFDoc_DocumentTool.ColorTool_s(doc.Main())

    roots = OCP.TDF.TDF_LabelSequence()
    st.GetFreeShapes(roots)
    if roots.Length() == 0:
        raise ConversionError("The STEP file contains no shapes")

    # ---------------------------------------------------------------- walk the assembly
    pn_counter: Counter = Counter()
    occurrences: list[dict] = []
    part_shapes: dict[str, object] = {}        # occ_id -> located TopoDS_Shape (parts only)
    product_cache: dict[int, dict] = {}        # product label hash -> geometry analysis
    unique_shapes: dict[int, tuple[str, object]] = {}   # product label -> (part number, shape): each meshed once
    root_pd = (structure.root_pds() or [None])[0]

    def color_of(label) -> list[float] | None:
        c = OCP.Quantity.Quantity_Color()
        for kind in (OCP.XCAFDoc.XCAFDoc_ColorSurf, OCP.XCAFDoc.XCAFDoc_ColorGen):
            try:
                if color_tool.GetColor(label, kind, c):
                    return [round(c.Red(), 3), round(c.Green(), 3), round(c.Blue(), 3)]
            except Exception:
                pass
        return None

    def resolve(parent_pd: int | None, instance_name: str, used: set[int]):
        """STEP usage (NAUO) for this XCAF component: by parent product definition + instance name."""
        if parent_pd is None:
            return None
        cands = [u for u in structure.children_of(parent_pd) if u.entity not in used]
        for key in ("id", "name"):
            for u in cands:
                if getattr(u, key) == instance_name:
                    return u
        base = instance_name.rsplit(".", 1)[0] if re.search(r"\.\d+$", instance_name) else instance_name
        for u in cands:                                  # "F6137-C75969" / "F6137-C75969.2": the child product's id
            p = structure.product_of_pd(u.child_pd)
            if p and base and p.id == base:
                return u
        return cands[0] if len(cands) == 1 else None

    def _fold(target, pd: int | None):
        """A sub-assembly that is really one part: bodies or manufacturing phases as sub-products.
        -> {"shape", "info"} or None. The GLB keeps the bodies; the finished one is named for the part and
        the others are hidden ("hidden:" names), so picking and the IPL see one part."""
        comps = OCP.TDF.TDF_LabelSequence()
        st.GetComponents_s(target, comps)
        kids = []
        for i in range(1, comps.Length() + 1):
            lab = comps.Value(i)
            tgt = OCP.TDF.TDF_Label()
            if not st.GetReferredShape_s(lab, tgt):
                continue
            if st.IsAssembly_s(tgt):
                return None                               # a real sub-assembly level below
            nm = (_label_name(OCP, lab) or "") + " " + (_label_name(OCP, tgt) or "")
            shp = st.GetShape_s(tgt).Moved(st.GetLocation_s(lab))
            props = _props(OCP, shp)
            kids.append({"label": lab, "name": nm.strip(), "shape": shp, "bbox": _bbox(OCP, shp),
                         "volume": props.get("volume_mm3") or 0.0})
        if not kids:
            return None
        no_usages = bool(structure.usages) and pd is not None and not structure.children_of(pd)
        named = all(PHASE_RE.search(k["name"].upper()) for k in kids)
        overlap = len(kids) >= 2 and all(_iou(kids[0]["bbox"], k["bbox"]) >= 0.5 for k in kids[1:]) and \
            len({re.sub(r"[._]\d+$", "", (k["name"].split() or [""])[0]) for k in kids}) == 1
        if no_usages:                                    # bodies of one part: all of them are the part
            keep = kids
            how = "bodies of one part (no STEP sub-products)"
        elif named or overlap:                            # manufacturing phases: keep the finished body
            solid = [k for k in kids if k["volume"] > 0]
            if not solid:
                return None
            big = max(k["volume"] for k in solid)
            final = [k for k in solid if FINAL_RE.search(k["name"].upper())]
            keep = [final[0] if final else min((k for k in solid if k["volume"] >= 0.05 * big), key=lambda k: k["volume"])]
            how = "manufacturing phases or bodies: the finished body is used"
        else:
            return None
        comp = OCP.TopoDS.TopoDS_Compound()
        bld = OCP.BRep.BRep_Builder()
        bld.MakeCompound(comp)
        for k in keep:
            bld.Add(comp, k["shape"])
        for k in kids:
            _set_label_name(OCP, k["label"], ("body:" if k in keep else "hidden:") + (k["name"].split() or ["body"])[0])
        return {"shape": comp, "info": {"bodies": len(kids), "kept": [k["name"] for k in keep], "how": how}}

    def walk(label, parent: dict | None, loc, pd: int | None, used_by_parent: set[int], path: list[str]):
        is_ref = st.IsReference_s(label)
        target = label
        if is_ref:
            target = OCP.TDF.TDF_Label()
            st.GetReferredShape_s(label, target)
        instance_name = _label_name(OCP, label) if is_ref else ""
        product_label_name = _label_name(OCP, target)

        usage = resolve(parent["_pd"] if parent else None, instance_name, used_by_parent) if parent else None
        if usage:
            used_by_parent.add(usage.entity)
        # no STEP usage found for a component: do not take the parent's product (the part would be named like
        # its assembly); the instance name says what it is
        this_pd = usage.child_pd if usage else (pd if parent is None else None)
        product = structure.product_of_pd(this_pd) if this_pd is not None else None
        pn_raw = product.id if product else (instance_name.rsplit(".", 1)[0] if instance_name else product_label_name)
        name = product.name if product else product_label_name
        desc = product.description if product else ""
        base = safe_pn(pn_raw)
        pn_counter[base] += 1
        occ_id = f"{base}_{pn_counter[base]}"

        comp_loc = st.GetLocation_s(label) if is_ref else OCP.TopLoc.TopLoc_Location()
        world = loc.Multiplied(comp_loc)
        is_asm = st.IsAssembly_s(target)
        occ = {
            "occ_id": occ_id, "parent": parent["occ_id"] if parent else None,
            "path": "/".join(path + [instance_name or pn_raw]),
            "instance_name": instance_name, "part_number": pn_raw, "name": name, "description": desc,
            "is_assembly": bool(is_asm), "depth": len(path), "world_transform": _trsf_matrix(OCP, world),
            "local_transform": _trsf_matrix(OCP, comp_loc), "children": [], "_pd": this_pd,
        }
        col = color_of(label) or color_of(target)
        if col:
            occ["color"] = col
        if is_ref:
            _set_label_name(OCP, label, occ_id)        # becomes the glTF node name
        else:
            _set_label_name(OCP, label, occ_id)
        occurrences.append(occ)
        if parent:
            parent["children"].append(occ_id)

        fold = _fold(target, this_pd) if is_asm and parent is not None else None
        if fold is not None:
            occ["is_assembly"] = False
            occ["folded"] = fold["info"]
        if is_asm and fold is None:
            comps = OCP.TDF.TDF_LabelSequence()
            st.GetComponents_s(target, comps)
            used: set[int] = set()
            for i in range(1, comps.Length() + 1):
                walk(comps.Value(i), occ, world, this_pd, used, path + [instance_name or pn_raw])
        else:
            shape = fold["shape"] if fold else st.GetShape_s(target)
            key = target.Tag()
            if key not in product_cache:
                kinds, feats = _features(OCP, shape)
                product_cache[key] = {"bbox_local": _bbox(OCP, shape), "face_types": kinds,
                                      "cylinders_local": feats, **_props(OCP, shape)}
                unique_shapes[key] = (pn_raw, shape)
                if len(product_cache) % 10 == 0:
                    prog(f"Reading geometry: {len(product_cache)} parts", min(0.34, 0.05 + 0.002 * len(product_cache)))
            g = product_cache[key]
            located = shape.Located(world)
            occ["geometry"] = {
                "bbox": _bbox(OCP, located), "volume_mm3": g.get("volume_mm3"), "area_mm2": g.get("area_mm2"),
                "face_types": g["face_types"],
                "cylinders": [_transform_feature(f, occ["world_transform"]) for f in g["cylinders_local"]],
            }
            if g.get("centroid"):
                c = np.array(occ["world_transform"]).reshape(4, 4) @ np.array([*g["centroid"], 1.0])
                occ["geometry"]["centroid"] = [round(float(v), 4) for v in c[:3]]
            part_shapes[occ_id] = located

    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i), None, OCP.TopLoc.TopLoc_Location(), root_pd, set(), [])

    # each distinct part once (instances share the mesh), so progress moves and a slow part is named in the log
    n_u = max(len(unique_shapes), 1)
    for k, (pn_u, shp) in enumerate(unique_shapes.values()):
        prog(f"Tessellating part {k + 1} of {n_u}", 0.35 + 0.2 * k / n_u)
        t_part = time.time()
        OCP.BRepMesh.BRepMesh_IncrementalMesh(shp, lin, False, ang, True)
        if time.time() - t_part > 10:
            log.warning("tessellating %s took %.0f s (a complex or faulty surface; quality: fast meshes it coarser)",
                        pn_u, time.time() - t_part)
    prog("Tessellating the assembly", 0.55)
    for i in range(1, roots.Length() + 1):          # anything not reached part by part (free shapes); already-meshed faces are skipped
        shp = st.GetShape_s(roots.Value(i))
        OCP.BRepMesh.BRepMesh_IncrementalMesh(shp, lin, False, ang, True)

    prog("Extracting meshes", 0.55)
    meshes = {}
    n_parts = max(len(part_shapes), 1)
    for k, (occ_id, shp) in enumerate(part_shapes.items()):
        OCP.BRepMesh.BRepMesh_IncrementalMesh(shp, lin, False, ang, True)
        v, f = _triangles(OCP, shp)
        meshes[occ_id] = (v.astype(np.float32), f.astype(np.int32))
        if k % 10 == 0:
            prog("Extracting meshes", 0.55 + 0.2 * k / n_parts)

    stem = safe_pn(structure.product_of_pd(root_pd).id) if root_pd and structure.product_of_pd(root_pd) else step_path.stem
    glb_path = out_dir / f"{stem}.glb"
    prog("Writing GLB", 0.8)
    writer = OCP.RWGltf.RWGltf_CafWriter(OCP.TCollection.TCollection_AsciiString(str(glb_path)), True)
    try:
        conv = writer.ChangeCoordinateSystemConverter()
        conv.SetInputLengthUnit(0.001)                # STEP millimetres -> glTF metres
        conv.SetInputCoordinateSystem(OCP.RWMesh.RWMesh_CoordinateSystem_Zup) if hasattr(OCP, "RWMesh") else None
    except Exception:
        pass
    ok = writer.Perform(doc, OCP.TColStd.TColStd_IndexedDataMapOfStringString(), OCP.Message.Message_ProgressRange())
    if not ok or not glb_path.exists():
        raise ConversionError("glTF export failed")

    # ---------------------------------------------------------------- outputs
    clean = []
    for o in occurrences:
        o = {k: v for k, v in o.items() if not k.startswith("_")}
        clean.append(o)
    by_id = {o["occ_id"]: o for o in clean}
    extras = {o["occ_id"]: {"occ_id": o["occ_id"], "part_number": o["part_number"], "name": o["name"],
                            "path": o["path"], "is_assembly": o["is_assembly"]} for o in clean}
    patched = _patch_glb(glb_path, extras, {"generator": "studio.cad.occ_converter", "source": step_path.name})

    # legacy-compatible flat BOM (leaf parts, instance counts) and assembly tree
    counts: dict[str, dict] = {}
    for o in clean:
        if not o["is_assembly"]:
            e = counts.setdefault(o["part_number"], {"partNumber": o["part_number"], "name": o["name"],
                                                     "description": o["description"], "instances": 0,
                                                     "material": None})
            e["instances"] += 1

    def tree(occ_id: str) -> dict:
        o = by_id[occ_id]
        return {"name": o["name"], "partNumber": o["part_number"], "isPart": not o["is_assembly"],
                "occ_id": occ_id, "children": [tree(c) for c in o["children"]]}

    root_id = clean[0]["occ_id"]
    model = {
        "format": "studio.product-model/1",
        "source": {"file": step_path.name, "schema": structure.header.schema,
                   "originating_system": structure.header.originating_system,
                   "preprocessor": structure.header.preprocessor, "time_stamp": structure.header.time_stamp},
        "units": "mm", "up_axis": "Z",
        "gltf": {"file": glb_path.name, "units": "m", "up_axis": "Y",
                 "note": "glTF = STEP converted to metres and Y-up: (x, y, z)_step -> (x, z, -y)/1000"},
        "root": root_id,
        "quality": quality,
        "occurrences": clean,
        "bom": sorted(counts.values(), key=lambda e: e["partNumber"]),
        "assembly_tree": tree(root_id),
        "stats": {"occurrences": len(clean), "parts": sum(1 for o in clean if not o["is_assembly"]),
                  "products": len({o["part_number"] for o in clean}), "glb_nodes_tagged": patched,
                  "seconds": round(time.monotonic() - t0, 2)},
    }
    model_path = out_dir / "product_model.json"
    model_path.write_text(json.dumps(model, indent=1), encoding="utf-8")
    np.savez_compressed(out_dir / "meshes.npz", **{f"{k}__v": v for k, (v, _) in meshes.items()},
                        **{f"{k}__f": f for k, (_, f) in meshes.items()})
    prog("Done", 1.0)
    log.info("Converted %s: %s", step_path.name, model["stats"])
    return {"glb": str(glb_path), "product_model": str(model_path), "meshes": str(out_dir / "meshes.npz"),
            "stats": model["stats"]}


def load_meshes(path: str | Path) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    z = np.load(path)
    out = {}
    for k in z.files:
        if k.endswith("__v"):
            occ = k[:-3]
            out[occ] = (z[k].astype(np.float64), z[occ + "__f"])
    return out
