"""
Matches a STEP-derived BOM against an MBOM/APL, producing a
confidence-scored reconciliation. Every rule here was derived from an
actual reconciliation run against real Safran APL + STEP file pairs.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from .mbom_loader import strip_hierarchy_dots, get_hierarchy_depth
from .nomenclature import token_similarity, cross_lingual_similarity, component_type_match
from .structural_context import (
    build_mbom_parent_map, build_step_parent_map,
    structural_match_score, is_nested_under_another_candidate,
)



# --- Part number normalization ---------------------------------------------

def _clean_pn_common(pn: str) -> str:
    """Cleanup applied symmetrically to both STEP and MBOM part numbers."""
    # Confirmed real case: STEP part number 'F6137-M83461/1-142 (AS5857)'
    # carries a trailing CATIA-added parenthetical annotation that never
    # appears in the MBOM at all — stripping it is what turns this into a
    # genuine exact match (MBOM's real row is plain 'M83461-1-142').
    pn = re.sub(r"\s*\([^)]*\)\s*$", "", pn)
    pn = pn.replace("/", "-")
    # Deliberate trailing-suffix stripping FIRST (instance/config markers,
    # e.g. "_L0", "_01") — these are specific, known patterns.
    pn = re.sub(r"_L\d+$", "", pn, flags=re.IGNORECASE)
    pn = re.sub(r"_\d+$", "", pn)
    # THEN a blanket underscore->dash pass for anything left over.
    # Confirmed real case: 'M83461_1-019' uses an underscore as a plain
    # separator mid-string (not a trailing suffix pattern), which the
    # rules above never touch — this catches that without disturbing the
    # deliberate suffix-stripping above, since those cases are already
    # handled and removed by the time this runs.
    pn = pn.replace("_", "-")
    return pn.strip()


def normalize_part_number(part_number: str, manuf_prefix: str = None,
                           root_product_number: str = None) -> str:
    pn = part_number.strip()
    if manuf_prefix and pn.upper().startswith(manuf_prefix.upper() + "-"):
        pn = pn[len(manuf_prefix) + 1:]
    pn, _ = strip_hierarchy_dots(pn)
    if root_product_number:
        suffix = f"-{root_product_number.strip().upper()}"
        if pn.upper().endswith(suffix):
            pn = pn[: -len(suffix)]
    return _clean_pn_common(pn)


def normalize_mbom_part_number(part_number: str) -> str:
    pn, _ = strip_hierarchy_dots(part_number.strip())
    return _clean_pn_common(pn)


def detect_dominant_prefix(step_bom: list) -> str:
    from collections import Counter
    prefixes = [item.get("partNumber", "").split("-", 1)[0].upper()
                for item in step_bom if "-" in item.get("partNumber", "")]
    if not prefixes:
        return None
    most_common_prefix, count = Counter(prefixes).most_common(1)[0]
    return most_common_prefix if count / len(prefixes) >= 0.5 else None

def resolve_trailing_letter_variants(unmatched_step, matched_by_key, manuf_prefix, root_product_number):
    """Post-processing pass, run after everything else. For items still
    unmatched after all tiers, checks whether stripping a single trailing
    letter from their normalized part number matches an ALREADY-matched
    item's key — and only merges if the resulting SUMMED quantity exactly
    equals what MBOM declares for that part.

    Confirmed real case: 'M83461-1-014_L0' (qty 1, matched to MBOM's
    'M83461-1-014' which declares qty 3) and 'M83461/1-014A' (qty 2,
    otherwise unmatched) sum to exactly 3.

    Deliberately requires exact quantity verification before merging —
    the earlier near-duplicate-typo tier merged purely on string
    proximity with no such check and caused a real false merge between
    two unrelated parts. This is both narrower (one trailing letter only,
    not a broad search) and safer (the sum must independently verify).
    """
    resolved_indices = []
    for idx, u in enumerate(unmatched_step):
        norm = normalize_part_number(u.step_part_number, manuf_prefix, root_product_number)
        if not norm or not norm[-1].isalpha():
            continue
        stripped_key = norm[:-1].rstrip("-").upper()
        if stripped_key in matched_by_key:
            existing = matched_by_key[stripped_key]
            if existing.mbom_qty is None:
                continue
            candidate_total = existing.step_qty + u.step_qty
            if candidate_total == existing.mbom_qty:
                existing.step_qty = candidate_total
                existing.member_part_numbers.append(u.step_part_number)
                existing.quantity_match = True
                existing.flags.append(
                    f"Resolved via trailing-letter variant: '{u.step_part_number}' (qty {u.step_qty}) "
                    f"summed with the existing match on '{stripped_key}' — combined total "
                    f"{candidate_total} matches MBOM's declared qty {existing.mbom_qty} exactly. "
                    f"Verify this is genuinely the same part before fully trusting."
                )
                resolved_indices.append(idx)
    for idx in sorted(resolved_indices, reverse=True):
        del unmatched_step[idx]
    return unmatched_step

# --- Part-number edit distance (typo detection) -----------------------------

def levenshtein(a: str, b: str) -> int:
    """Small, dependency-free edit distance — these are short part number
    strings, no need for a library. Confirmed real case: STEP part number
    'M83641/1-009' is a single digit-transposition typo away from the
    real MBOM part 'M83461-1-009' — nomenclature-only matching missed
    this entirely and guessed a wrong, unrelated part instead.
    """
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j-1] + 1, prev[j-1] + cost)
        prev = curr
    return prev[-1]


def find_near_duplicate_part_number(norm_pn: str, mbom_by_pn: dict, used_mbom_keys: set,
                                     max_distance: int = 2) -> tuple:
    """Returns (mbom_rec, distance) for the closest near-identical MBOM
    part number within max_distance edits, or (None, None). Deliberately
    checked BEFORE nomenclature-only fuzzy matching — a near-identical
    part number is a much stronger, more specific signal than generic
    nomenclature overlap, and should win over it.
    """
    best_rec, best_dist = None, max_distance + 1
    for pn_key, rec in mbom_by_pn.items():
        if pn_key in used_mbom_keys or is_likely_process_spec(rec):
            continue
        if abs(len(pn_key) - len(norm_pn)) > max_distance:
            continue
        dist = levenshtein(norm_pn.upper(), pn_key)
        if 0 < dist <= max_distance and dist < best_dist:
            best_rec, best_dist = rec, dist
    return (best_rec, best_dist) if best_rec else (None, None)


# --- Placeholder detection --------------------------------------------------

PLACEHOLDER_PATTERNS = [
    re.compile(r"^TEST\d*$", re.IGNORECASE),
    re.compile(r"^TBD$", re.IGNORECASE),
    re.compile(r"^PLACEHOLDER$", re.IGNORECASE),
    re.compile(r"^XXX+$", re.IGNORECASE),
    re.compile(r"^\d{1,3}$"),
]


def is_placeholder_part_number(part_number: str) -> bool:
    pn = part_number.strip()
    return any(p.match(pn) for p in PLACEHOLDER_PATTERNS)


# --- Supersession parsing ---------------------------------------------------

SUPERSESSION_PATTERN = re.compile(
    r"\{?\s*([A-Z0-9\-\.]+)\s*/\s*"
    r"(REMPLACE|EST UNE EVOLUTION DE)\s*\([^)]*\)\s*/\s*"
    r"([A-Z0-9\-\.]+)\s*\}?",
    re.IGNORECASE,
)


def parse_supersession(comments: str) -> dict:
    if not comments:
        return None
    m = SUPERSESSION_PATTERN.search(comments)
    if not m:
        return None
    referenced_pn, relation, other_pn = m.groups()
    return {
        "referenced_part_number": referenced_pn.strip(),
        "other_part_number": other_pn.strip(),
        "relation": relation.strip().upper(),
    }


def build_supersession_index(mbom_records: list) -> dict:
    index = {}
    for rec in mbom_records:
        sup = parse_supersession(rec.get("comments", ""))
        if sup:
            index[sup["referenced_part_number"].upper()] = rec
            index[sup["other_part_number"].upper()] = rec
    return index


# --- Process-spec filtering --------------------------------------------------

NON_PHYSICAL_PART_PREFIXES = ("NCT", "IFC", "PCS-")


def is_likely_process_spec(mbom_record: dict) -> bool:
    pn = mbom_record.get("part_number", "").lstrip(".").upper()
    return pn.startswith(NON_PHYSICAL_PART_PREFIXES)


# --- Multi-row MBOM aggregation ---------------------------------------------

def build_mbom_by_pn_grouped(mbom_records: list) -> dict:
    """Groups ALL MBOM rows sharing the same normalized part number (not
    just the last one seen). Confirmed real case: 'EQ8734-004' appears as
    two separate rows (qty 2 + qty 1) at different assembly positions,
    summing to exactly the STEP-found total of 3 — comparing against only
    one row was the actual cause of that mismatch, not a real
    discrepancy.
    """
    groups = defaultdict(list)
    for rec in mbom_records:
        clean = normalize_mbom_part_number(rec.get("part_number", ""))
        groups[clean.upper()].append(rec)
    return dict(groups)


def summed_qty(records: list) -> int:
    total, any_valid = 0, False
    for rec in records:
        qty_raw = rec.get("qty", "")
        try:
            total += int(float(qty_raw)) if qty_raw != "" else 0
            any_valid = True
        except ValueError:
            pass
    return total if any_valid else None

def find_embedded_part_number(norm_pn: str, mbom_by_pn: dict, used_mbom_keys: set):
    """Checks whether norm_pn CONTAINS a real MBOM part number as a
    contiguous dash-separated token subsequence — e.g. 'GD69046-REP'
    contains 'GD69046'; 'FILS-BOBINES-GD69041' contains 'GD69041' as its
    last token, regardless of position (prefix, suffix, or middle).

    Confirmed real need: the general weighted last-resort scorer picks
    the WRONG candidate for cases like this — generic semantic/structural
    signals from an unrelated but similarly-described part (e.g.
    'ROTOR RECTIFIE' pulling toward 'EQUIPPED GROUND ROTOR' instead of the
    real match 'ROTOR SHAFT') outweigh a much stronger, highly specific
    part-number correspondence. This checks that correspondence directly,
    as its own dedicated signal, instead of diluting it into an average.
    No hardcoded suffix/prefix patterns — works for any decoration as
    long as the real part number survives as a clean dash-token.
    """
    parts = norm_pn.upper().split("-")
    for i in range(len(parts)):
        for j in range(i + 1, len(parts) + 1):
            candidate = "-".join(parts[i:j])
            if candidate == norm_pn.upper():
                continue  # the whole string — that's Tier 1's job, not this
            if candidate in mbom_by_pn and candidate not in used_mbom_keys and not is_likely_process_spec(mbom_by_pn[candidate]):
                return mbom_by_pn[candidate], candidate
    return None, None

# --- Variant groups (J/K style interchangeable options) ---------------------

def build_variant_groups(mbom_records: list, mbom_parent_map: dict) -> dict:
    """Groups MBOM rows sharing the same (parent, Seq. Number) into
    'variant slots'. Confirmed real bug: grouping by seq_number ALONE is
    wrong — 'Seq. No.' resets/repeats independently under each parent
    assembly in this report format, so unrelated rows under DIFFERENT
    parents that happen to share the same local seq number string were
    being incorrectly treated as interchangeable variants of each other.
    Confirmed real case: MS21209F4-15/NAS1130-4FL15 (seq '200', under
    parent GD69029) and MS21209F4-20/NAS1130-4FL20 (seq '200', under
    parent GD69030) are two DIFFERENT variant slots in two different
    sub-assemblies that only coincidentally share the same seq number.
    """
    groups = defaultdict(list)
    for rec in mbom_records:
        if is_likely_process_spec(rec):
            continue
        seq = rec.get("seq_number")
        if not seq:
            continue
        own_pn = strip_hierarchy_dots(rec.get("part_number", ""))[0].upper()
        parent = mbom_parent_map.get(own_pn)
        groups[(parent, seq)].append(rec)
    return {k: recs for k, recs in groups.items() if len(recs) > 1}


def variant_siblings_for(mbom_rec: dict, variant_groups: dict, mbom_parent_map: dict) -> list:
    seq = mbom_rec.get("seq_number")
    if not seq:
        return []
    own_pn = strip_hierarchy_dots(mbom_rec.get("part_number", ""))[0].upper()
    parent = mbom_parent_map.get(own_pn)
    key = (parent, seq)
    if key not in variant_groups:
        return []
    siblings = []
    for rec in variant_groups[key]:
        rec_pn = strip_hierarchy_dots(rec.get("part_number", ""))[0]
        if rec_pn.upper() == own_pn:
            continue
        qty_raw = rec.get("qty", "")
        try:
            qty = int(float(qty_raw)) if qty_raw != "" else None
        except ValueError:
            qty = None
        siblings.append({
            "equiv": rec.get("equiv"), "part_number": rec_pn,
            "description": rec.get("description"), "qty": qty,
        })
    return siblings

def effective_quantity(pn_upper: str, mbom_by_pn_grouped: dict, variant_groups: dict,
                        mbom_parent_map: dict, _visiting: set = None) -> int:
    """Resolves a part's REAL, usable quantity — not just its raw declared
    qty. If a part's own declared qty is 0 but it has a variant sibling
    with a nonzero qty (the J/K pattern), returns the sibling's qty
    instead. This is what makes ancestor-multiplication safe: multiplying
    by a raw qty=0 "inactive variant" ancestor produces a nonsensical 0,
    but resolving to the ancestor's actually-active sibling first gives a
    real, usable multiplier.
    """
    _visiting = _visiting or set()
    if pn_upper in _visiting:
        return 1  # guard against any pathological cycle
    _visiting.add(pn_upper)

    records = mbom_by_pn_grouped.get(pn_upper)
    if not records:
        return 1
    qty = summed_qty(records)
    if qty:
        return qty

    # Own quantity is 0/unknown — check variant siblings at the same slot.
    for rec in records:
        for sib in variant_siblings_for(rec, variant_groups, mbom_parent_map):
            sib_pn = strip_hierarchy_dots(sib["part_number"])[0].upper()
            if sib.get("qty"):
                return sib["qty"]
    return qty or 1


def ancestor_multiplier(pn_upper: str, mbom_parent_map: dict, mbom_by_pn_grouped: dict,
                         variant_groups: dict, max_depth: int = 15) -> int:
    """Product of the EFFECTIVE quantity of every ancestor up the MBOM
    hierarchy (not counting the part's own row, which is compared
    separately). Confirmed real case: '41320677006' has parent
    'MS21209F0615' with MBOM qty 11 — 1(own) x 11(ancestor) = 11, matching
    STEP's found total of 11 exactly.
    """
    product = 1
    current = mbom_parent_map.get(pn_upper)
    depth = 0
    while current and depth < max_depth:
        product *= effective_quantity(current, mbom_by_pn_grouped, variant_groups, mbom_parent_map)
        current = mbom_parent_map.get(current)
        depth += 1
    return product

# --- Result structure --------------------------------------------------------

@dataclass
class MatchResult:
    step_part_number: str
    step_name: str
    step_qty: int
    status: str
    confidence: float
    mbom_part_number: str = None
    mbom_description: str = None
    mbom_qty: int = None
    mbom_manuf_code: str = None
    mbom_level: int = None
    quantity_match: bool = None
    flags: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    variant_siblings: list = field(default_factory=list)
    effectivity_code: str = None
    member_part_numbers: list = field(default_factory=list)


WEIGHTS = {
    "structural": 0.30, "semantic": 0.25, "component_type": 0.20,
    "qty": 0.10, "spec": 0.10, "part_number_sim": 0.05,
}
AI_SEMANTIC_WEIGHT = 0.20


def spec_match_score(step_material, mbom_material_spec):
    if not step_material or not mbom_material_spec:
        return 0.0
    return token_similarity(step_material, mbom_material_spec)

def group_step_bom(step_bom: list, manuf_prefix: str = None, root_product_number: str = None) -> list:
    """Groups STEP items that normalize to the same part number, summing
    their instance counts before matching. Confirmed real case:
    'M83461_1-019' and 'M83461/1-019_L1' are two separate STEP leaf
    entries (each instances=1) representing the same nominal MBOM part
    'M83461-1-019' (qty 2) — comparing each individually produced two
    false '1 != 2' mismatches; combined, they correctly sum to 2 == 2.
    """
    groups = {}
    for item in step_bom:
        # .get(key, "") only falls back when the KEY is absent — if the
        # JSON has "partNumber": null explicitly (real case: parts where
        # the C++ tool's entity lookup never resolved a name), .get()
        # still returns None and crashes downstream on the first
        # .strip() call. `or ""` catches both missing-key AND
        # explicit-None uniformly.
        item_pn = item.get("partNumber") or ""
        item_name = item.get("name") or ""

        norm_pn = normalize_part_number(item_pn, manuf_prefix, root_product_number)
        key = norm_pn.upper()
        if key not in groups:
            groups[key] = {
                "partNumber": item_pn,
                "name": item_name,
                "material": item.get("material"),
                "instances": 0,
                "_member_part_numbers": [],
            }
        groups[key]["instances"] += item.get("instances", 1)
        groups[key]["_member_part_numbers"].append(item_pn)
    return list(groups.values())


def score_candidate(step_qty, step_part_number, step_name, step_material, norm_step_pn,
                     step_parent_map, mbom_rec, mbom_parent_map, mbom_records_by_pn,
                     semantic_scorer=None):
    mbom_pn = strip_hierarchy_dots(mbom_rec.get("part_number", ""))[0]
    mbom_qty_raw = mbom_rec.get("qty", "")
    try:
        mbom_qty = int(float(mbom_qty_raw)) if mbom_qty_raw != "" else None
    except ValueError:
        mbom_qty = None

    signals = {
        "structural": structural_match_score(step_part_number, step_parent_map, mbom_pn, mbom_parent_map, mbom_records_by_pn),
        "semantic": token_similarity(step_name, mbom_rec.get("description", "")),
        "component_type": component_type_match(step_name, mbom_rec.get("description", "")),
        "qty": 1.0 if (mbom_qty is not None and mbom_qty == step_qty) else 0.0,
        "spec": spec_match_score(step_material, mbom_rec.get("material_spec", "")),
        "part_number_sim": SequenceMatcher(None, norm_step_pn.upper(), mbom_pn.upper()).ratio(),
    }

    weights = dict(WEIGHTS)
    if semantic_scorer is not None:
        signals["ai_semantic"] = semantic_scorer(step_name, mbom_rec.get("description", ""))
        weights = {k: v * (1 - AI_SEMANTIC_WEIGHT) for k, v in weights.items()}
        weights["ai_semantic"] = AI_SEMANTIC_WEIGHT

    combined = sum(signals[k] * weights[k] for k in signals)
    return combined, signals


def format_candidate_note(mbom_rec, score, signals):
    mbom_pn = strip_hierarchy_dots(mbom_rec.get("part_number", ""))[0]
    sig_str = " ".join(f"{k}:{v:.2f}" for k, v in signals.items())
    return f"{mbom_pn} ({mbom_rec.get('description','')}, {sig_str}, score={score:.2f})"


def reconcile(step_bom, mbom_records, manuf_prefix=None, root_product_number=None,
              fuzzy_threshold=0.85, fuzzy_ambiguity_margin=0.1, assembly_tree=None,
              semantic_scorer=None, cross_lingual_threshold=0.5, expected_variant=None,
              step_shape_descriptors=None, sibling_nomenclature_threshold=0.5,
              part_number_max_edit_distance=2):
    """
    sibling_nomenclature_threshold: raised from 0.3 to 0.5 — confirmed
        real false positives at 0.3-0.33 where a single shared GENERIC
        component-type word (e.g. "RING", "INSERT") alone cleared the old
        bar, incorrectly flagging unrelated parts as "same branch,
        related nomenclature" siblings.
    """
    if manuf_prefix is None:
        manuf_prefix = detect_dominant_prefix(step_bom)

    step_bom = group_step_bom(step_bom, manuf_prefix, root_product_number)

    mbom_by_pn_grouped = build_mbom_by_pn_grouped(mbom_records)
    mbom_by_pn = {k: v[0] for k, v in mbom_by_pn_grouped.items()}  # representative record per key, for description/level/etc.

    supersession_index = build_supersession_index(mbom_records)
    mbom_parent_map = build_mbom_parent_map(mbom_records)
    step_parent_map = build_step_parent_map(assembly_tree) if assembly_tree else {}
    variant_groups = build_variant_groups(mbom_records, mbom_parent_map)

    matched, unmatched_step = [], []
    used_mbom_keys = set()
    matched_by_key = {}  # matched_key -> MatchResult, for the merge-on-typo-match logic above

    for step_item in step_bom:
        step_pn_raw = step_item.get("partNumber", "")
        step_name = step_item.get("name", "")
        step_qty = step_item.get("instances", 1)
        step_material = (step_item.get("material") or {}).get("name", "")
        member_pns = step_item.get("_member_part_numbers", [step_pn_raw])

        norm_pn = normalize_part_number(step_pn_raw, manuf_prefix, root_product_number)
        mbom_rec = mbom_by_pn.get(norm_pn.upper())
        status, confidence, flags, candidates = "matched", 1.0, [], []
        matched_key = norm_pn.upper() if mbom_rec else None

        if mbom_rec is not None:
            own_pn = strip_hierarchy_dots(mbom_rec.get("part_number", ""))[0].upper()
            for pn_key, rec in mbom_by_pn.items():
                if pn_key == own_pn or is_likely_process_spec(rec):
                    continue
                rec_qty_raw = rec.get("qty", "")
                try:
                    rec_qty = int(float(rec_qty_raw)) if rec_qty_raw != "" else None
                except ValueError:
                    rec_qty = None
                same_qty = rec_qty == step_qty
                same_branch = mbom_parent_map.get(pn_key) == mbom_parent_map.get(own_pn)
                nomenclature_related = token_similarity(step_name, rec.get("description", "")) >= sibling_nomenclature_threshold
                rec_equiv = (rec.get("equiv") or "").strip().upper()
                is_different_variant = (expected_variant is not None and rec_equiv and rec_equiv != expected_variant.strip().upper())

                if same_qty and same_branch and nomenclature_related:
                    if is_different_variant:
                        flags.append(f"Note: '{rec.get('part_number')}' (equiv '{rec.get('equiv')}') is a different variant than expected '{expected_variant}' — likely fine as-is.")
                    else:
                        flags.append(f"Exact match found on '{mbom_rec.get('part_number')}', but '{rec.get('part_number')}' ({rec.get('description')}) sits in the same assembly branch with matching quantity ({step_qty}) and related nomenclature — verify which part number is currently active.")
                        candidates.append({"mbom_part_number": strip_hierarchy_dots(rec.get("part_number", ""))[0], "description": rec.get("description"), "qty": rec_qty, "level": get_hierarchy_depth(rec), "reason": "same branch, matching quantity, related nomenclature, different part number"})

        # Tier 2: supersession
        if mbom_rec is None:
            sup_rec = supersession_index.get(norm_pn.upper())
            if sup_rec:
                mbom_rec = sup_rec
                status = "matched_via_supersession"
                confidence = 0.9
                matched_key = strip_hierarchy_dots(sup_rec.get("part_number", ""))[0].upper()
                flags.append(f"STEP part number '{norm_pn}' is superseded in MBOM by '{sup_rec.get('part_number')}'.")

        if is_placeholder_part_number(norm_pn):
            flags.append(f"STEP part number '{step_pn_raw}' matches a known placeholder pattern.")
            if mbom_rec is None:
                status, confidence = "placeholder_suspected", 0.0

        # Tier 2.5: near-identical part number (typo detection).
        # Finds the closest candidate WITHOUT excluding already-used keys
        # first — confirmed real case: 'M83461-1-014A' is genuinely
        # closest to 'M83461-1-014' (distance 1), but that row was
        # already consumed by a separate exact match, and excluding it
        # outright caused a worse, wrong fallback ('M83461-1-142',
        # distance 2) to be picked instead. If the true closest candidate
        # is already matched, this merges into that existing result's
        # instance count (same logic as STEP-side grouping above) rather
        # than either blocking it or accepting a worse guess.
        #
        # Deliberately does NOT decide FOR you whether a trailing-letter
        # difference means "same part, extra instance" or "genuinely
        # distinct authorized variant" — aerospace part numbering uses
        # both conventions in real files. This surfaces the closest
        # candidate and flags it for confirmation either way, rather than
        # silently guessing.
        if mbom_rec is None:
            near_rec, dist = find_near_duplicate_part_number(
                norm_pn, mbom_by_pn, set(), part_number_max_edit_distance  # no used-key exclusion here
            )
            if near_rec is not None:
                near_key = strip_hierarchy_dots(near_rec.get("part_number", ""))[0].upper()
                if near_key not in used_mbom_keys:
                    mbom_rec = near_rec
                    status = "fuzzy_candidate"
                    confidence = round(1.0 - (dist / max(len(norm_pn), 1)) * 0.5, 2)
                    matched_key = near_key
                    flags.append(
                        f"Part number '{step_pn_raw}' is {dist} character(s) different from MBOM's "
                        f"'{near_rec.get('part_number')}' — could be the same part with a typo, or a "
                        f"genuinely distinct variant. Verify before trusting."
                    )

        # Tier 2.6: embedded real part number, verified by exact quantity
        # agreement. Confirmed real cases: STEP decorates a genuinely
        # correct part number with extra prefix/suffix text ('GD69046-REP',
        # 'FILS-BOBINES-GD69041') that a diluted weighted-average score
        # gets wrong, but which resolves cleanly and confidently once
        # checked directly and confirmed against the real declared quantity.
        if mbom_rec is None:
            embedded_rec, embedded_key = find_embedded_part_number(norm_pn, mbom_by_pn, used_mbom_keys)
            if embedded_rec is not None:
                embedded_qty_raw = embedded_rec.get("qty", "")
                try:
                    embedded_qty = int(float(embedded_qty_raw)) if embedded_qty_raw != "" else None
                except ValueError:
                    embedded_qty = None
                if embedded_qty is not None and embedded_qty == step_qty:
                    mbom_rec = embedded_rec
                    status = "fuzzy_candidate"
                    confidence = 0.75
                    matched_key = embedded_key
                    flags.append(
                        f"STEP part number '{step_pn_raw}' contains real MBOM part '{embedded_key}' "
                        f"as an embedded token, with matching quantity ({step_qty}) confirming the "
                        f"correspondence. The surrounding text ('{step_pn_raw}') may indicate a "
                        f"specific lifecycle state or sub-reference — verify before fully trusting."
                    )


        # Tier 3: nomenclature-gated fuzzy
        if mbom_rec is None:
            pool = [(k, r) for k, r in mbom_by_pn.items() if k not in used_mbom_keys and not is_likely_process_spec(r) and token_similarity(step_name, r.get("description", "")) >= fuzzy_threshold]
            pool_pns = [k for k, _ in pool]
            pool = [(k, r) for k, r in pool if not is_nested_under_another_candidate(k, pool_pns, mbom_parent_map)]
            scored = []
            for k, r in pool:
                combined, signals = score_candidate(step_qty, step_pn_raw, step_name, step_material, norm_pn, step_parent_map, r, mbom_parent_map, mbom_by_pn, semantic_scorer)
                scored.append((combined, r, signals, k))
            scored.sort(key=lambda x: -x[0])

            if len(scored) == 1 or (len(scored) > 1 and scored[0][0] - scored[1][0] >= fuzzy_ambiguity_margin):
                best_score, best_rec, best_signals, best_key = scored[0]
                mbom_rec, status, confidence, matched_key = best_rec, "fuzzy_candidate", round(best_score, 2), best_key
                flags.append(f"No part-number match — best candidate is {format_candidate_note(best_rec, best_score, best_signals)}.")
            elif len(scored) > 1:
                ranked_notes = "; ".join(format_candidate_note(r, s, sig) for s, r, sig, _ in scored[:4])
                ranked_candidates = [{"mbom_part_number": strip_hierarchy_dots(r.get("part_number", ""))[0], "description": r.get("description"), "score": round(s, 2), "level": get_hierarchy_depth(r), "signals": {k: round(v, 2) for k, v in sig.items()}} for s, r, sig, _ in scored[:4]]
                unmatched_step.append(MatchResult(step_part_number=step_pn_raw, step_name=step_name, step_qty=step_qty, status="ambiguous", confidence=0.0, flags=[f"Multiple candidates too close: {ranked_notes}."], candidates=ranked_candidates))
                continue

        # Tier 4: cross-lingual
        if mbom_rec is None:
            cross_pool = [(k, r) for k, r in mbom_by_pn.items() if k not in used_mbom_keys and not is_likely_process_spec(r) and cross_lingual_similarity(step_name, r.get("description", "")) >= cross_lingual_threshold]
            cross_pool_pns = [k for k, _ in cross_pool]
            cross_pool = [(k, r) for k, r in cross_pool if not is_nested_under_another_candidate(k, cross_pool_pns, mbom_parent_map)]
            cross_scored = []
            for k, r in cross_pool:
                combined, signals = score_candidate(step_qty, step_pn_raw, step_name, step_material, norm_pn, step_parent_map, r, mbom_parent_map, mbom_by_pn, semantic_scorer)
                signals["cross_lingual"] = round(cross_lingual_similarity(step_name, r.get("description", "")), 2)
                cross_scored.append((combined, r, signals, k))
            cross_scored.sort(key=lambda x: -x[0])
            if cross_scored and (len(cross_scored) == 1 or cross_scored[0][0] - cross_scored[1][0] >= fuzzy_ambiguity_margin):
                best_score, best_rec, best_signals, best_key = cross_scored[0]
                mbom_rec, status, confidence, matched_key = best_rec, "translated_candidate", round(best_score * 0.75, 2), best_key
                flags.append(f"Cross-language candidate — {format_candidate_note(best_rec, best_score, best_signals)}.")

        # Tier 5: last-resort weighted structural/semantic match. Confirmed
        # real case: STEP 'D56700-0331' ("COMPRESSION SPRING - DYNAMIC")
        # has NO corresponding part number anywhere in the MBOM at all
        # (independently verified against the raw STEP entity graph — this
        # is a real, confirmed assembly child of the root, not a text
        # coincidence) — and its nomenclature overlap with the true match
        # ('SPRING') is too weak (0.33) to clear Tiers 3/4's thresholds.
        # This tier removes the nomenclature gate entirely and scores
        # EVERY remaining candidate on the full weighted signal set
        # (structural/semantic/component-type/qty/spec/part-number),
        # accepting only when the top candidate has a clear, wide margin
        # over the next-best — deliberately the most conservative tier,
        # since it's the last thing tried before giving up.
        if mbom_rec is None:
            last_resort_pool = [
                (k, r) for k, r in mbom_by_pn.items()
                if k not in used_mbom_keys and not is_likely_process_spec(r)
            ]
            last_resort_scored = []
            for k, r in last_resort_pool:
                combined, signals = score_candidate(
                    step_qty, step_pn_raw, step_name, step_material, norm_pn,
                    step_parent_map, r, mbom_parent_map, mbom_by_pn, semantic_scorer,
                )
                last_resort_scored.append((combined, r, signals, k))
            last_resort_scored.sort(key=lambda x: -x[0])

            if last_resort_scored:
                best_score, best_rec, best_signals, best_key = last_resort_scored[0]
                second_score = last_resort_scored[1][0] if len(last_resort_scored) > 1 else 0.0
                clear_margin = best_score - second_score >= 0.15
                qty_agrees = best_signals.get("qty", 0.0) == 1.0
                if best_score >= 0.45 and clear_margin and qty_agrees:
                    mbom_rec = best_rec
                    status = "last_resort_candidate"
                    confidence = round(best_score * 0.6, 2)
                    matched_key = best_key
                    flags.append(
                        f"No part number, nomenclature, or cross-language match found. Best "
                        f"structural/semantic candidate (last resort): "
                        f"{format_candidate_note(best_rec, best_score, best_signals)}, margin over "
                        f"next-best: {best_score - second_score:.2f}. Requires manual confirmation "
                        f"before trusting — this is the least certain match tier."
                    )

        # Tier 6: shape-based suggestion, geometry only, no text signal at
        # all. Self-gated on corpus size — confirmed real case: with only
        # 20 confirmed corpus entries, the TRUE correct sibling for a
        # BACK-UP RING query ranked 4th of 5 nearest neighbors, not 1st.
        # Retrieval works mechanically at that size, but isn't reliably
        # RANKED yet. Stays dormant below the threshold rather than risk
        # confidently wrong suggestions, and activates automatically once
        # there's enough real confirmed data to trust — no manual
        # "turning it on" needed later.
        if mbom_rec is None and step_shape_descriptors is not None:
            from .shape_learning.corpus import corpus_size
            MIN_CORPUS_SIZE_FOR_SHAPE_MATCHING = 200
            if corpus_size() >= MIN_CORPUS_SIZE_FOR_SHAPE_MATCHING:
                from .shape_learning.retrieval import find_nearest
                query_desc = step_shape_descriptors.get(step_pn_raw)
                if query_desc is not None:
                    neighbors = find_nearest(query_desc, k=1)
                    if neighbors and neighbors[0]["distance"] < 1.0:
                        nb = neighbors[0]
                        nb_key = nb["part_number"].upper()
                        candidate = mbom_by_pn.get(nb_key)
                        if candidate and nb_key not in used_mbom_keys:
                            mbom_rec = candidate
                            status = "shape_suggested"
                            confidence = 0.3
                            matched_key = nb_key
                            flags.append(
                                f"No text-based match found at all. Closest shape match in the "
                                f"confirmed-parts corpus: '{nb['part_number']}' ({nb['description']}, "
                                f"distance {nb['distance']:.2f}). This is geometry-only evidence — "
                                f"requires manual confirmation."
                            )

        if mbom_rec is None:
            unmatched_step.append(MatchResult(step_part_number=step_pn_raw, step_name=step_name, step_qty=step_qty, status="unmatched", confidence=0.0, flags=["No corresponding MBOM line found."] + flags))
            continue

        final_score, final_signals = score_candidate(step_qty, step_pn_raw, step_name, step_material, norm_pn, step_parent_map, mbom_rec, mbom_parent_map, mbom_by_pn, semantic_scorer)
        if status in ("matched", "matched_via_supersession"):
            confidence = round(max(confidence, final_score), 2)

        used_mbom_keys.add(matched_key)

        # Quantity comparison now checks THREE things in order: (1) the
        # summed quantity across all MBOM rows sharing this part number,
        # (2) if that still doesn't match, whether multiplying by the
        # ancestor chain's effective quantity explains it, (3) if still
        # not, the existing variant-sibling check, (4) otherwise a real
        # flagged mismatch.
        grouped_records = mbom_by_pn_grouped.get(matched_key, [mbom_rec])
        mbom_qty = summed_qty(grouped_records)

        qty_match = (mbom_qty == step_qty) if mbom_qty is not None else None
        siblings, effectivity_code = [], None

        if qty_match is False:
            multiplier = ancestor_multiplier(matched_key, mbom_parent_map, mbom_by_pn_grouped, variant_groups)
            expected_with_multiplier = (mbom_qty or 0) * multiplier
            if multiplier > 1 and expected_with_multiplier == step_qty:
                qty_match = True
                flags.append(
                    f"Quantity resolved via parent-assembly multiplier: MBOM declares {mbom_qty} "
                    f"per parent instance, and the parent chain effectively installs this "
                    f"{multiplier}x — {mbom_qty} x {multiplier} = {step_qty}, matching STEP exactly."
                )
            else:
                group_siblings = variant_siblings_for(mbom_rec, variant_groups, mbom_parent_map)
                resolving_sibling = next((s for s in group_siblings if s.get("qty") == step_qty), None)
                if resolving_sibling:
                    qty_match = True
                    effectivity_code = resolving_sibling.get("equiv")
                    siblings = [resolving_sibling]
                    flags.append(f"Quantity resolved via variant match: STEP qty {step_qty} matches the '{resolving_sibling['equiv']}' variant ({resolving_sibling['part_number']}), not '{mbom_rec.get('equiv')}'.")
                elif group_siblings:
                    siblings = group_siblings
                    sibling_str = ", ".join(f"{s['equiv']} ({s['part_number']}, qty {s['qty']})" for s in group_siblings)
                    flags.append(f"Quantity mismatch: STEP has {step_qty}, MBOM has {mbom_qty} (equiv '{mbom_rec.get('equiv')}'). Checked alternates: {sibling_str} — none match.")
                else:
                    flags.append(f"Quantity mismatch: STEP has {step_qty}, MBOM has {mbom_qty}.")

        result_obj = MatchResult(
            step_part_number=step_pn_raw, step_name=step_name, step_qty=step_qty,
            status=status, confidence=confidence,
            mbom_part_number=strip_hierarchy_dots(mbom_rec.get("part_number", ""))[0],
            mbom_description=mbom_rec.get("description"), mbom_qty=mbom_qty,
            mbom_manuf_code=mbom_rec.get("manuf_code"), mbom_level=get_hierarchy_depth(mbom_rec),
            quantity_match=qty_match, flags=flags, candidates=candidates,
            signals={k: round(v, 2) for k, v in final_signals.items()},
            variant_siblings=siblings, effectivity_code=effectivity_code,
            member_part_numbers=member_pns,
        )
        matched.append(result_obj)
        matched_by_key[matched_key] = result_obj

    unmatched_step = resolve_trailing_letter_variants(unmatched_step, matched_by_key, manuf_prefix, root_product_number)
    unmatched_mbom = [
        {**rec, "level_depth": get_hierarchy_depth(rec)}
        for rec in mbom_records
        if strip_hierarchy_dots(rec.get("part_number", ""))[0].upper() not in used_mbom_keys
    ]

    return {"matched": matched, "unmatched_step": unmatched_step, "unmatched_mbom": unmatched_mbom}

