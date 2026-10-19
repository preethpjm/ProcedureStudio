"""
Reconstructs "who is this part's parent" on both sides of the reconciliation,
so structural position can be used as a matching signal — not just part
number and nomenclature.

Also derives EFFECTIVE (whole-top-assembly) quantities for MBOM lines by
multiplying down the hierarchy, since the raw Qty column on a multi-level
MBOM/APL is per-immediate-parent only, not a total (see
compute_effective_quantities below).

Confirmed real cases this addresses:
- MS21209F4-15, NAS1130-4FL15, and MS124694 all sit at the same MBOM level
  under the same parent (GD69029) — genuinely structural siblings, so
  structural matching alone won't disambiguate between them (quantity does).
- C72151 (level 1, direct child of the top assembly) vs EBC72151 (level 2,
  nested UNDER C72151 itself) are NOT structural siblings — EBC72151 is a
  sub-reference inside C72151's own branch, not a competing top-level
  candidate. This case IS resolved by structural filtering.
- A Safran APL (F6137-D24903000) has parts nested 3-5 levels deep with a
  qty > 1 at intermediate levels (e.g. WOUND STATOR under the top assembly
  qty=1, but its own child LAMINATION STACK holds 152 laminations, and a
  magnet 4 levels down is qty=3 per rotor while the rotor itself repeats).
  Comparing STEP's already-fully-multiplied instance count against that
  MBOM row's raw, un-multiplied qty produces false "quantity mismatch"
  flags on entirely correct parts. compute_effective_quantities fixes this.
"""

from .mbom_loader import strip_hierarchy_dots, get_hierarchy_depth

def build_mbom_parent_map(mbom_records: list) -> dict:
    """Returns {clean_part_number: parent_clean_part_number_or_None}.

    Reconstructed from row order + hierarchy depth via a simple stack:
    each row's parent is the nearest preceding row with a shallower depth.
    Depth comes from get_hierarchy_depth(), which handles both known
    conventions (leading dots on the part number, or a separate
    asterisk-based Niveau column).
    """
    parent_map = {}
    stack = []  # list of (depth, clean_part_number)

    for rec in mbom_records:
        pn = rec.get("part_number", "")
        clean, _ = strip_hierarchy_dots(pn)
        clean = clean.upper()
        depth = get_hierarchy_depth(rec)

        while stack and stack[-1][0] >= depth:
            stack.pop()

        parent_map[clean] = stack[-1][1] if stack else None
        stack.append((depth, clean))

    return parent_map


def _raw_qty(rec: dict):
    """Parses a single row's own Qty cell. Returns None if blank/non-numeric
    (distinct from 0, which is a real, meaningful value — e.g. a Find No.
    with qty 0 in a real APL meaning "not fitted on this variant")."""
    raw = rec.get("qty", "")
    try:
        return int(float(raw)) if raw != "" else None
    except (ValueError, TypeError):
        return None


def compute_effective_quantities(mbom_records: list, mbom_parent_map: dict) -> dict:
    """Returns {clean_part_number: effective_total_qty}.

    A multi-level MBOM/APL's Qty column is PER IMMEDIATE PARENT, not a
    total against the top assembly — e.g. "3 ea NORTH MAGNET" means 3 per
    rotor, and if that rotor itself only occurs once per top assembly the
    true total is 3, but if some ancestor further up also repeats (qty>1),
    the true total is 3 times THAT ancestor's own effective quantity too.

    This walks each part's ancestor chain (via mbom_parent_map, itself
    built in build_mbom_parent_map) from the part up to the root,
    multiplying every level's own raw qty together, so the result is the
    true count of that part needed to build ONE complete top assembly —
    directly comparable to a STEP/CAD converter's already-fully-multiplied
    "instances" count (see reconciler.py).

    A missing/non-numeric qty at any level (own row OR an ancestor) is
    treated as a multiplier of 1, not as "unknown"/aborting the chain.
    Confirmed real case: the top-level assembly row itself (Lev 0, e.g.
    D24903000) and several process/finish-spec rows have a BLANK Qty
    column entirely on this document, which conventionally means "one",
    not "not applicable" — silently bailing out on a blank ancestor qty
    would incorrectly discard the effective quantity of every part
    underneath it in the tree, which is the opposite of what's needed
    here (this function must always return a usable total).
    """
    own_qty = {}
    for rec in mbom_records:
        pn, _ = strip_hierarchy_dots(rec.get("part_number", ""))
        own_qty[pn.upper()] = _raw_qty(rec)

    effective = {}
    for pn in own_qty:
        total = 1
        current = pn
        depth_guard = 0
        while current and depth_guard < 30:  # guards against a corrupt/cyclic parent map
            q = own_qty.get(current)
            total *= q if q is not None else 1
            current = mbom_parent_map.get(current)
            depth_guard += 1
        effective[pn] = total

    return effective


def build_step_parent_map(assembly_tree: dict) -> dict:
    """Returns {step_part_number: nearest_ancestor_name}, walked from the
    converter's assembly_tree JSON. Uses the ancestor's NAME (e.g.
    'EQUIPED BODY') rather than its raw XCAF label id, since that's what's
    comparable against MBOM nomenclature — MBOM has no knowledge of STEP's
    internal label ids at all.

    Requires the part-level 'partNumber' field to be present on tree nodes
    matching the flat bom's partNumber (same converter output, so it is).
    """
    parent_map = {}

    def walk(node, ancestor_name):
        node_name = node.get("name")
        if node.get("isPart"):
            pn = node.get("partNumber")
            if pn:
                parent_map[pn] = ancestor_name
        for child in node.get("children", []):
            walk(child, node_name if not node.get("isPart") else ancestor_name)

    walk(assembly_tree, None)
    return parent_map


def structural_match_score(step_part_number: str, step_parent_map: dict,
                            mbom_candidate_pn: str, mbom_parent_map: dict,
                            mbom_records_by_pn: dict) -> float:
    """Returns 0.0-1.0. Compares the STEP part's ancestor NAME against the
    MBOM candidate's ancestor part number's nomenclature — fuzzy, since the
    two sides have no shared identifier for assembly-level containers
    (STEP has no MBOM part number for e.g. 'EQUIPED BODY', only a name).
    """
    from difflib import SequenceMatcher

    step_parent_name = step_parent_map.get(step_part_number)
    mbom_parent_pn = mbom_parent_map.get(mbom_candidate_pn.upper())

    if not step_parent_name or not mbom_parent_pn:
        return 0.0

    mbom_parent_rec = mbom_records_by_pn.get(mbom_parent_pn.upper())
    if not mbom_parent_rec:
        return 0.0

    mbom_parent_desc = mbom_parent_rec.get("description", "")
    return SequenceMatcher(None, step_parent_name.upper(), mbom_parent_desc.upper()).ratio()


def is_nested_under_another_candidate(candidate_pn: str, other_candidate_pns: list,
                                       mbom_parent_map: dict) -> bool:
    """True if candidate_pn's parent chain passes through another candidate
    in the same pool — i.e. it's a sub-reference nested inside a sibling
    candidate's own branch, not an independently competing match.

    Confirmed real case: EBC72151 is nested directly under C72151, and both
    appeared as tied candidates for the same STEP VALVE part. EBC72151
    should be excluded/demoted in favor of C72151 here.
    """
    current = mbom_parent_map.get(candidate_pn.upper())
    depth_guard = 0
    while current and depth_guard < 10:
        if current in {p.upper() for p in other_candidate_pns}:
            return True
        current = mbom_parent_map.get(current)
        depth_guard += 1
    return False

