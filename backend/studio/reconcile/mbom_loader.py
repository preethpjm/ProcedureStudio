"""
Loads an MBOM/APL document from either Excel/CSV or PDF into a common list
of dicts, using column_mapping.py to identify fields regardless of exact
header wording or column order.

Returns: list[dict], each dict having whatever canonical fields were found
(level, part_number, description, qty, manuf_code, comments, ...).
Missing fields are simply absent from the dict, not filled with None,
so downstream code should use .get() with sensible defaults.
"""

import re
from pathlib import Path
from .column_mapping import build_index_map, DEFAULT_COLUMN_MAP


def _clean_cell(value) -> str:
    if value is None:
        return ""
    # pandas represents empty Excel cells as float NaN, not None — str(nan)
    # is the literal string "nan", which is truthy and was corrupting every
    # sheet's data (empty cells counted as real values) until this check
    # was added. Confirmed real bug on an actual multi-sheet Excel workbook,
    # not present with the PDF loader since pdfplumber returns real None
    # for missing cells instead.
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return ""
    except TypeError:
        pass
    return str(value).strip()

def load_from_pasted_text(text: str, column_map: dict = None) -> list:
    """Parses tabular data pasted directly by a user (e.g. copied from
    Excel) — tab-separated by default (Excel's actual clipboard format),
    falling back to comma-separated if no tabs are found at all. Reuses
    the same header-detection/row-parsing logic as the file-based
    loaders, tested directly against both delimiter styles.
    """
    column_map = column_map or DEFAULT_COLUMN_MAP
    raw_lines = [line for line in text.splitlines() if line.strip()]
    if not raw_lines:
        raise ValueError("No data found in pasted text.")

    delimiter = "\t" if "\t" in raw_lines[0] else ","
    rows = [line.split(delimiter) for line in raw_lines]

    header_row_idx = None
    for i, row in enumerate(rows[:10]):
        if _looks_like_header_row(row, column_map):
            header_row_idx = i
            break
    if header_row_idx is None:
        raise ValueError(
            "Could not find a header row with a recognizable Part Number "
            "column in the pasted data. Make sure the first row (or one "
            "of the first few) contains column headers, or use the "
            "downloadable template for guaranteed formatting."
        )

    index_map = build_index_map(rows[header_row_idx], column_map)
    records = []
    for row in rows[header_row_idx + 1:]:
        rec = _row_to_record(row, index_map)
        if rec.get("part_number"):
            records.append(rec)
    return records

def _row_to_record(row: list, index_map: dict) -> dict:
    record = {}
    for field, idx in index_map.items():
        if idx < len(row):
            val = _clean_cell(row[idx])
            if val:
                record[field] = val
    return record


def _looks_like_header_row(row: list, column_map: dict) -> bool:
    idx_map = build_index_map(row, column_map)
    # Require part_number AND at least one other real BOM field —
    # confirmed real case: a row containing only a "Code Article" cell
    # (matching part_number alone) sat above the actual header row in a
    # real multi-block Excel sheet, and got wrongly accepted as the
    # header when only part_number was required. That misaligned every
    # column for every row below it, including the real header row.
    if "part_number" not in idx_map:
        return False
    return any(f in idx_map for f in ("description", "qty"))


def load_from_excel_or_csv(path: str, column_map: dict = None, sheet_name: str = None) -> list:
    """sheet_name: force a specific sheet. If omitted, every sheet is
    scanned and the best-scoring one is used automatically.

    Confirmed necessary on a real 19-sheet production workbook: most
    sheets are NOT flat BOM tables at all (wide per-seat-position
    quantity matrices, packaging lists, workstation timing sheets), and
    sheet NAMING is not reliable either — a sheet named "EBOM report
    23-09" in that file turned out to be a manufacturing timing extract,
    not a BOM, despite the name strongly suggesting otherwise. Only
    scoring actual header content per sheet catches this correctly.
    """
    import pandas as pd

    column_map = column_map or DEFAULT_COLUMN_MAP
    path_obj = Path(path)
    is_csv = path_obj.suffix.lower() == ".csv"

    if is_csv:
        sheets = {"__csv__": pd.read_csv(path, header=None).values.tolist()}
    elif sheet_name:
        sheets = {sheet_name: pd.read_excel(path, sheet_name=sheet_name, header=None).values.tolist()}
    else:
        xls = pd.read_excel(path, sheet_name=None, header=None)
        sheets = {name: df.values.tolist() for name, df in xls.items()}

    best_name, best_records, best_field_count, best_row_count = None, [], -1, -1

    for name, rows in sheets.items():
        header_row_idx = None
        for i, row in enumerate(rows[:20]):  # header may sit deeper than 10 rows in multi-block sheets
            if _looks_like_header_row(row, column_map):
                header_row_idx = i
                break
        if header_row_idx is None:
            continue

        index_map = build_index_map(rows[header_row_idx], column_map)
        records = []
        for row in rows[header_row_idx + 1:]:
            rec = _row_to_record(row, index_map)
            if rec.get("part_number"):
                records.append(rec)

        # Primary score: how many distinct canonical fields the header
        # recognized — richer header = more likely a real BOM export, not
        # a derived report. Row count is only a tiebreaker. Confirmed
        # necessary: a per-seat-position "matrix" sheet naturally has far
        # MORE rows than the actual hierarchical EBOM (every component
        # repeats once per seat position), so row count alone picked the
        # wrong sheet even though it had fewer distinct recognized fields
        # (4 vs the real EBOM sheet's 5, including "level" — which
        # matrix-style sheets never have at all).
        field_count = len(index_map)
        row_count = len(records)
        if (field_count, row_count) > (best_field_count, best_row_count):
            best_name, best_records = name, records
            best_field_count, best_row_count = field_count, row_count

    if best_row_count <= 0:
        raise ValueError(
            "Could not find a header row with a recognizable Part Number "
            "column on any sheet. Check column_mapping.py — the actual "
            "header text in this file may need a variant added, or pass "
            "sheet_name explicitly if you know which sheet has the BOM."
        )

    return best_records


def load_from_pdf(path: str, column_map: dict = None) -> list:
    """Best-effort PDF table extraction via pdfplumber. Column POSITION
    (not just header text) is used to align data rows to fields.

    IMPORTANT: the header row is re-detected independently on EVERY page,
    not just once for the whole document. Confirmed against a real 3-page
    Safran APL report that different pages can have different column
    counts/layouts even within the same document. Reusing one page's
    index map on another page's rows silently misaligns every field —
    this must not be "optimized" into a single global header detection.

    Recommend spot-checking the first run's output against the source PDF
    for any new BOM format before trusting it fully — PDF table extraction
    quality varies by exporting tool.
    """
    import pdfplumber

    column_map = column_map or DEFAULT_COLUMN_MAP
    records = []
    pages_with_no_header = []

    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables()
            for table in tables:
                index_map = None
                for row in table:
                    if index_map is None:
                        if _looks_like_header_row(row, column_map):
                            index_map = build_index_map(row, column_map)
                        continue
                    rec = _row_to_record(row, index_map)
                    if rec.get("part_number"):
                        records.append(rec)
                if index_map is None:
                    pages_with_no_header.append(page_num)

    if not records:
        raise ValueError(
            "Could not find a header row with a recognizable Part Number "
            "column anywhere in the PDF tables. This BOM format may need "
            "a new header variant added to column_mapping.py, or the PDF's "
            "table structure may not be extractable by pdfplumber at all "
            "(e.g. a scanned/image-based PDF, which would need OCR first)."
        )
    if pages_with_no_header:
        import warnings
        warnings.warn(
            f"No header row found on page(s) {pages_with_no_header} — "
            f"any data rows on those pages were skipped entirely rather "
            f"than risk misaligned fields. Spot-check those pages manually."
        )

    return records


def load_mbom(path: str, column_map: dict = None, sheet_name: str = None) -> list:
    """Auto-detects format by extension and loads accordingly.
    sheet_name only applies to .xlsx/.xls — ignored/irrelevant for .csv/.pdf.
    """
    suffix = Path(path).suffix.lower()
    if suffix in (".xlsx", ".xls", ".csv"):
        return load_from_excel_or_csv(path, column_map, sheet_name)
    elif suffix == ".pdf":
        return load_from_pdf(path, column_map)
    else:
        raise ValueError(f"Unsupported MBOM file type: {suffix}. Expected .xlsx, .xls, .csv, or .pdf")


def strip_hierarchy_dots(part_number: str) -> tuple:
    """Some MBOM exports (confirmed on a real Safran APL) prefix part
    numbers with leading dots indicating hierarchy depth, e.g. '..C75970'
    for a level-2 item. Returns (clean_part_number, dot_count) so dot_count
    can be used as a redundant cross-check against an explicit Level column
    when both are present. Returns dot_count=0 for part numbers using a
    different depth convention — see get_hierarchy_depth for the unified
    entry point.
    """
    match = re.match(r"^(\.*)(.*)$", part_number)
    dots, clean = match.group(1), match.group(2)
    return clean.strip(), len(dots)


def get_hierarchy_depth(record: dict) -> int:
    """Unified depth extraction across the two conventions seen so far:
    - Leading dots directly on the part number (Safran APL PDF format)
    - A separate 'level' column using asterisks, e.g. '**' = depth 2
      (confirmed real case: a French production EBOM export using
      Niveau='*'/'**'/'***' instead of encoding depth into the part
      number string at all)

    Extend here, not by guessing in structural_context.py, if a third
    convention shows up — this is the one place depth interpretation
    should live.
    """
    pn = record.get("part_number", "")
    _, dot_depth = strip_hierarchy_dots(pn)
    if dot_depth > 0:
        return dot_depth

    level_raw = record.get("level", "")
    if level_raw and set(level_raw.strip()) == {"*"}:
        return len(level_raw.strip())

    try:
        return int(float(level_raw))
    except (ValueError, TypeError):
        return 0

