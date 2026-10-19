"""ATA iSpec 2200 CMM (XML form): DISASSEMBLY (pgblk 3000), ASSEMBLY (pgblk 7000) and the IPL (pgblk 10000).

Structure and attribute conventions follow ASTHRA's RA-7100 reference CMM: ``task`` /
``topic`` / ``subtask`` with ``prclist1`` / ``prcitem1`` / ``prcitem`` / ``para``, warnings and
cautions at subtask level, ``ted`` (tools) and ``con`` (consumables) inline, and the IPL as
``dplist`` / ``figure`` / ``prtlist`` / ``itemdata`` with keyword-adjective nomenclature.
"""
from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

from .config import PublicationConfig


def _a(**kw) -> str:
    return "".join(f" {k}={quoteattr(str(v))}" for k, v in kw.items() if v is not None and v != "")


def _common(cfg: PublicationConfig, pgblk: str, revdate: str) -> dict:
    return {"chapnbr": cfg.ata_chapter, "sectnbr": cfg.ata_section, "subjnbr": cfg.ata_subject,
            "pgblknbr": pgblk, "chg": "n", "revdate": revdate}


def _proc_pgblk(proc: dict, cfg: PublicationConfig, revdate: str) -> str:
    dis = proc["kind"] == "disassembly"
    pg, tfunc, sfunc, title = ("3000", "000", "040", "DISASSEMBLY") if dis else ("7000", "400", "420", "ASSEMBLY")
    c = _common(cfg, pg, revdate)
    key = f"{cfg.ata_chapter}{cfg.ata_section}{cfg.ata_subject}"
    safety = proc.get("safety", {})
    subs = []
    for i, st in enumerate(proc["subtasks"], start=1):
        wc = "".join(f"<{safety[w]['kind']}><para>{escape(safety[w]['text'])}</para></{safety[w]['kind']}>"
                     for w in st.get("warnings", []) + st.get("cautions", []) if w in safety)
        items = "".join(f"<prcitem1><prcitem><para>{escape(s['text'])}</para></prcitem></prcitem1>" for s in st["steps"])
        subs.append(f"<subtask{_a(**{**c, 'func': sfunc, 'seq': f'{i:03d}', 'confltr': 'A', 'varnbr': '1', 'key': f'subtask-{key}-{sfunc}-{i:03d}-a01'})}>"
                    f"<title>{escape(st['title'])}</title>{wc}<prclist1>{items}</prclist1></subtask>")
    pre = proc["preliminary"]
    pre_items = []
    for w in pre.get("warnings", []):
        if w in safety:
            pre_items.append(f"<{safety[w]['kind']}><para>{escape(safety[w]['text'])}</para></{safety[w]['kind']}>")
    lines = []
    for s in pre.get("support_equipment", []):
        lines.append(f"<prcitem1><prcitem><para>Use the <ted><toolnbr>{escape(s['id'].upper())}</toolnbr>"
                     f"<toolname>{escape(s['name'])}</toolname></ted>.</para></prcitem></prcitem1>")
    for s in pre.get("supplies", []):
        lines.append(f"<prcitem1><prcitem><para>Get the <con><connbr>{escape(s['spec'])}</connbr>"
                     f"<conname>{escape(s['name'])}</conname></con>.</para></prcitem></prcitem1>")
    for s in pre.get("spares", []):
        lines.append(f"<prcitem1><prcitem><para>Get {s['qty']} new {escape(s['name'])} ({escape(s['item'])}), "
                     f"{escape(s['part_number'])}.</para></prcitem></prcitem1>")
    for p in proc.get("common_info", []):
        lines.append(f"<prcitem1><prcitem><para>{escape(p)}</para></prcitem></prcitem1>")
    prep = ""
    if pre_items and not lines:
        lines.append("<prcitem1><prcitem><para>Obey the warning before you start the procedure.</para></prcitem></prcitem1>")
    if lines or pre_items:
        prep = (f"<subtask{_a(**{**c, 'func': sfunc, 'seq': '000', 'confltr': 'A', 'varnbr': '1', 'key': f'subtask-{key}-{sfunc}-000-a01'})}>"
                f"<title>Preparation</title>{''.join(pre_items)}<prclist1>{''.join(lines)}</prclist1></subtask>")
    task = (f"<task{_a(**{**c, 'func': tfunc, 'seq': '801', 'confltr': 'A', 'varnbr': '1', 'key': f'task-{key}-{tfunc}-801-a01'})}>"
            f"<title>{title.title()}</title><topic>{prep}{''.join(subs)}</topic></task>")
    return f"<pgblk{_a(**{**c, 'key': f'pgblk-{key}-{pg}'})}><title>{title}</title>{task}</pgblk>"


def _ipl(ipl: dict, cfg: PublicationConfig, revdate: str, title: str) -> str:
    c = _common(cfg, "10000", revdate)
    key = f"{cfg.ata_chapter}{cfg.ata_section}{cfg.ata_subject}"
    items = []
    for r in ipl["rows"]:
        if r.get("status") == "rejected" or not r.get("item"):
            continue
        nom = r["nomenclature"]
        kwd, adt = (nom.split(",", 1) + [""])[:2] if "," in nom else (nom, "")
        msc = ""
        if r.get("torque"):
            t = r["torque"]
            msc = f"<msc>TORQUE {t['nominal_nm']:g} N.M" + (f" +/- {t['tolerance_nm']:g}" if t.get("tolerance_nm") else "") + "</msc>"
        upa = r.get("units_per_assy")
        upa_s = "RF" if r["depth"] == 0 else ("AR" if upa is None else str(upa))
        indent = max(r["indent"] - 1, 0)
        illus = "1" if r.get("occ_ids") else "0"
        items.append(f"<itemdata{_a(itemnbr=r['item'] if r['depth'] else '1', attach='1' if r.get('attaching_for') else '0', illusind=illus, indent=str(indent), delitem='0')}>"
                     f"<pnr>{escape(r['part_number'])}</pnr>"
                     f"<iplnom><nom><kwd>{escape(kwd.strip())}</kwd>" + (f"<adt>{escape(adt.strip())}</adt>" if adt.strip() else "")
                     + f"</nom>{msc}</iplnom>"
                     + (f"<effcode>{escape(r['effectivity'])}</effcode>" if r.get("effectivity") else "")
                     + (f"<mfr>V{escape(r['cage'])}</mfr>" if r.get("cage") and r["depth"] else "")
                     + f"<upa>{upa_s}</upa></itemdata>")
    return (f"<ipl{_a(**{**c, 'key': 'ipl-1'})}><title>ILLUSTRATED PARTS LIST</title><dplist><title>DETAILED PARTS LIST</title>"
            f"<figure{_a(**{**c, 'func': '950', 'seq': '1', 'confltr': 'A', 'varnbr': '1', 'fignbr': cfg.figure_number, 'key': f'figure-{key}-1'})}>"
            f"<title>FIGURE {escape(cfg.figure_number)} - {escape(title.upper())}</title>"
            f"<prtlist{_a(chg='n', key='prtlist-1', revdate=revdate)}>{''.join(items)}</prtlist></figure></dplist></ipl>")


def render_cmm(procs: list[dict], ipl: dict, cfg: PublicationConfig, title: str) -> tuple[str, str]:
    revdate = (cfg.issue_date or "").replace("-", "")
    top = next((r for r in ipl["rows"] if r["depth"] == 0), None)
    partinfo = ""
    if top:
        partinfo = (f"<partinfo><cmpnom>{escape(title.upper())}</cmpnom><mfrpnr><pnr>{escape(top['part_number'])}</pnr>"
                    f"<mfr>{escape(top.get('cage') or cfg.enterprise_code)}</mfr></mfrpnr></partinfo>")
    blocks = "".join(_proc_pgblk(p, cfg, revdate) for p in sorted(procs, key=lambda p: p["kind"] != "disassembly"))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           f"<cmm{_a(spl=cfg.enterprise_code, oidate=revdate, tsn='0', docnbr=cfg.cmm_doc_number or (top or {}).get('part_number', ''), lang=cfg.language, chapnbr=cfg.ata_chapter, sectnbr=cfg.ata_section, subjnbr=cfg.ata_subject, type='cmm', prelim='1', chg='n', revdate=revdate)}>"
           f"<title>COMPONENT MAINTENANCE MANUAL WITH ILLUSTRATED PARTS LIST</title>{partinfo}{blocks}{_ipl(ipl, cfg, revdate, title)}</cmm>\n")
    name = f"CMM-{cfg.ata_chapter}-{cfg.ata_section}-{cfg.ata_subject}_{(top or {}).get('part_number', 'IPL')}.xml"
    return name, xml
