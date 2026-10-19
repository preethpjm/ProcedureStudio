"""
Canonical MBOM field -> list of header text variants that map to it.

This is intentionally kept separate from the loading/matching logic. If a
different BOM format shows up (different column names, different provider),
extend this dict rather than touching mbom_loader.py or reconciler.py.

Matching against actual header text is done case-insensitively and after
stripping whitespace/newlines (PDF-extracted headers often come back with
embedded newlines from vertically-stacked column labels, e.g. "L\\ne\\nv").
"""

DEFAULT_COLUMN_MAP = {
    "level": ["Lev", "Level", "Lev v", "Niveau"],
    "find_number": ["Find Number", "FindNumber", "Find No", "Find No."],
    "seq_number": ["Seq. Number", "Seq Number", "Sequence", "Seq No", "Seq"],
    "equiv": ["Equiv", "Equivalence"],
    "part_number": ["Part Number", "PN", "Part No", "Part No.", "REF_ARTICLE", "Article", "Code Article"],
    # Explicit CAD/3D model cross-reference field, distinct from the
    # manufacturing part number — confirmed real case: a production MBOM
    # had BOTH "REF_ARTICLE" (the manufacturing part number, populated on
    # every row) AND "Reference du 3D" (only populated on some rows,
    # specifically meant to record the corresponding CAD/3D reference) as
    # separate columns. Kept separate rather than conflated.
    "cad_reference": ["Reference du 3D", "Ref 3D", "3D Reference"],
    "amdt": ["Amdt", "Amendment", "Révision", "Revision"],
    "description": ["Description", "Nomenclature", "Désignation FR", "Désignation EN"],
    "qty": ["Qty.", "Qty", "Quantity", "Quantité(L)", "Quantité", "Qté/SS"],
    "unit_measure": ["Unit Measure", "UOM", "Unit", "Unité de mesure(L)"],
    "material_spec": ["Material Specification", "Material Spec", "Material", "MATIERE_GENERIQUE"],
    "comments": ["Comments", "Comment", "Remarks", "Notes"],
    "class": ["Class", "TYPE_COMPOSANT"],
    "manuf_code": ["Manuf. Code", "Manufacturer Code", "Manuf Code", "CAGE", "CAGE Code"],
    "status": ["Status", "Etat", "État"],
    "authority": ["Authority"],
}

def get_template_headers() -> list:
    """One clean, canonical header string per recognized field — the
    exact text a user's own spreadsheet should use for guaranteed clean
    parsing, bypassing all the format-detection/inference logic needed
    for arbitrary real-world exports."""
    return [variants[0] for variants in DEFAULT_COLUMN_MAP.values()]


def normalize_header_text(text) -> str:
    """Collapses PDF-extracted vertical-text headers like 'L\\ne\\nv' -> 'lev',
    and normal headers like 'Manuf. Code' -> 'manuf. code', for matching."""
    if text is None:
        return ""
    return "".join(str(text).split()).lower().strip()


def build_index_map(header_row: list, column_map: dict = None) -> dict:
    """Given a raw header row (list of cell values, possibly with None gaps
    and embedded newlines), returns {canonical_field: column_index}.

    Only fields actually found in this header row are included — callers
    should treat missing fields as "not available in this document" rather
    than erroring, since not every MBOM export has every column.
    """
    column_map = column_map or DEFAULT_COLUMN_MAP
    normalized_variants = {
        canonical: {normalize_header_text(v) for v in variants}
        for canonical, variants in column_map.items()
    }

    result = {}
    for idx, cell in enumerate(header_row):
        norm = normalize_header_text(cell)
        if not norm:
            continue
        for canonical, variants in normalized_variants.items():
            if norm in variants and canonical not in result:
                result[canonical] = idx
                break
    return result

