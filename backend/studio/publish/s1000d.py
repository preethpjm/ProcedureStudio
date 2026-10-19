"""S1000D Issue 4.2 renderers: procedural data modules (530 disassemble, 710 assemble) and IPD (941).

Element order and content follow the S1000D 4.2 ``proced.xsd`` / ``ipd.xsd`` content models
(the same structure as ASTHRA's RA-7100 reference data modules). Empty requirement groups
use the schema's ``noSupportEquips`` / ``noSupplies`` / ``noSpares`` / ``noSafety`` elements.
Item numbers are written as three-digit IPD items (``220`` -> ``220``, ``10`` -> ``010``).
"""
from __future__ import annotations

import re
from datetime import date
from xml.sax.saxutils import escape, quoteattr

from .config import PublicationConfig

INFO = {"disassembly": ("530", "Disassemble procedure"), "assembly": ("710", "Assemble procedure"),
        "ipd": ("941", "Illustrated parts data")}


def _a(**kw) -> str:
    return "".join(f" {k}={quoteattr(str(v))}" for k, v in kw.items() if v is not None and v != "")


def _dmcode(cfg: PublicationConfig, info: str) -> str:
    return f"<dmCode{_a(**cfg.dmc(info))}/>"


def _id(prefix: str, s: str) -> str:
    return f"{prefix}-" + re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _ident_status(cfg: PublicationConfig, info: str, info_name: str, tech_name: str) -> str:
    y, m, d = (cfg.issue_date or date.today().isoformat()).split("-")
    ent = f"<enterpriseName>{escape(cfg.enterprise_name or cfg.enterprise_code)}</enterpriseName>"
    applic = escape(cfg.applicability or tech_name)
    code = " ".join(f'{k}="{escape(v)}"' for k, v in cfg.brex_dmcode().items())
    brex = f"<brexDmRef><dmRef><dmRefIdent><dmCode {code}/></dmRefIdent></dmRef></brexDmRef>"
    return (
        "<identAndStatusSection><dmAddress><dmIdent>"
        f"{_dmcode(cfg, info)}<language{_a(languageIsoCode=cfg.language, countryIsoCode=cfg.country)}/>"
        f"<issueInfo{_a(issueNumber=cfg.issue_number, inWork=cfg.in_work)}/></dmIdent>"
        f"<dmAddressItems><issueDate{_a(year=y, month=m, day=d)}/>"
        f"<dmTitle><techName>{escape(tech_name)}</techName><infoName>{escape(info_name)}</infoName></dmTitle>"
        "</dmAddressItems></dmAddress>"
        f'<dmStatus issueType="new"><security{_a(securityClassification=cfg.security_classification)}/>'
        f"<responsiblePartnerCompany{_a(enterpriseCode=cfg.enterprise_code)}>{ent}</responsiblePartnerCompany>"
        f"<originator{_a(enterpriseCode=cfg.enterprise_code)}>{ent}</originator>"
        f"<applic><displayText><simplePara>{applic}</simplePara></displayText></applic>"
        f"{brex}<qualityAssurance><unverified/></qualityAssurance></dmStatus></identAndStatusSection>"
    )


def _wc_para(text: str) -> str:
    return f"<warningAndCautionPara>{escape(text)}</warningAndCautionPara>"


def render_procedure(proc: dict, cfg: PublicationConfig, tech_name: str) -> tuple[str, str]:
    """Return (file name, XML) for an S1000D 4.2 procedural data module."""
    info, info_name = INFO[proc["kind"]]
    safety = proc.get("safety", {})
    used = set(proc["preliminary"].get("warnings", []))
    for st in proc["subtasks"]:
        used |= set(st.get("warnings", [])) | set(st.get("cautions", []))
    wc_ids = {sid: f"{'wrn' if safety[sid]['kind'] == 'warning' else 'cau'}-{i + 1:04d}"
              for i, sid in enumerate(sorted(used)) if sid in safety}
    wac = ""
    if wc_ids:
        wac = "<warningsAndCautions>" + "".join(
            f"<{safety[s]['kind']} id={quoteattr(wc_ids[s])}>{_wc_para(safety[s]['text'])}</{safety[s]['kind']}>"
            for s in sorted(wc_ids)) + "</warningsAndCautions>"
    pre = proc["preliminary"]
    persons = "".join(f'<personnel numRequired="{p["count"]}"><personCategory personCategoryCode={quoteattr(p["category"])}/></personnel>'
                      for p in pre.get("persons", []))
    se = pre.get("support_equipment", [])
    support = ("<reqSupportEquips><supportEquipDescrGroup>" + "".join(
        f"<supportEquipDescr id={quoteattr(s['id'])}><name>{escape(s['name'])}</name>"
        f'<reqQuantity unitOfMeasure="EA">{s.get("qty", 1)}</reqQuantity></supportEquipDescr>' for s in se)
        + "</supportEquipDescrGroup></reqSupportEquips>") if se else "<reqSupportEquips><noSupportEquips/></reqSupportEquips>"
    su = pre.get("supplies", [])
    supplies = ("<reqSupplies><supplyDescrGroup>" + "".join(
        f"<supplyDescr id={quoteattr(s['id'])}><name>{escape(s['name'])}</name>"
        f"<identNumber><manufacturerCode>{escape(cfg.enterprise_code or 'ZZZZZ')}</manufacturerCode>"
        f"<partAndSerialNumber><partNumber>{escape(s['spec'])}</partNumber></partAndSerialNumber></identNumber>"
        f"</supplyDescr>" for s in su)
        + "</supplyDescrGroup></reqSupplies>") if su else "<reqSupplies><noSupplies/></reqSupplies>"
    sp = pre.get("spares", [])
    spares = ("<reqSpares><spareDescrGroup>" + "".join(
        f"<spareDescr id={quoteattr(s['id'])}><name>{escape(s['name'])}</name>"
        f"<identNumber><manufacturerCode>{escape(s.get('cage') or cfg.enterprise_code or 'ZZZZZ')}</manufacturerCode>"
        f"<partAndSerialNumber><partNumber>{escape(s['part_number'])}</partNumber></partAndSerialNumber></identNumber>"
        f'<reqQuantity unitOfMeasure="EA">{s["qty"]}</reqQuantity></spareDescr>' for s in sp)
        + "</spareDescrGroup></reqSpares>") if sp else "<reqSpares><noSpares/></reqSpares>"
    pw = [w for w in pre.get("warnings", []) if w in safety]
    safety_x = ("<reqSafety><safetyRqmts>"
                + "".join(f"<{safety[w]['kind']}>{_wc_para(safety[w]['text'])}</{safety[w]['kind']}>" for w in pw)
                + "</safetyRqmts></reqSafety>") if pw else "<reqSafety><noSafety/></reqSafety>"
    prelim = (f"<preliminaryRqmts><reqCondGroup><noConds/></reqCondGroup><reqPersons>{persons}</reqPersons>"
              f"{support}{supplies}{spares}{safety_x}</preliminaryRqmts>")

    steps_x = []
    for k, st in enumerate(proc["subtasks"], start=1):
        refs = {}
        w = " ".join(wc_ids[x] for x in st.get("warnings", []) if x in wc_ids)
        c = " ".join(wc_ids[x] for x in st.get("cautions", []) if x in wc_ids)
        if w:
            refs["warningRefs"] = w
        if c:
            refs["cautionRefs"] = c
        inner = []
        for s in st["steps"]:
            if s["kind"] == "note":
                inner.append(f"<proceduralStep id={quoteattr(_id('stp', s['id']))}><para>{escape(s['text'])}</para></proceduralStep>")
            else:
                inner.append(f"<proceduralStep id={quoteattr(_id('stp', s['id']))}><para>{escape(s['text'])}</para></proceduralStep>")
        steps_x.append(f"<proceduralStep{_a(id=_id('stp', st['id']), **refs)}><title>{escape(st['title'])}</title>"
                       + "".join(inner) + "</proceduralStep>")
    common = ""
    paras = [f"This procedure gives the instructions to {'disassemble' if proc['kind'] == 'disassembly' else 'assemble'} "
             f"the {tech_name.lower()}."] + list(proc.get("common_info", []))
    common = "<commonInfo><title>Job description</title>" + "".join(f"<para>{escape(p)}</para>" for p in paras) + "</commonInfo>"
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<dmodule xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xlink="http://www.w3.org/1999/xlink" '
           'xsi:noNamespaceSchemaLocation="http://www.s1000d.org/S1000D_4-2/xml_schema_flat/proced.xsd">'
           + _ident_status(cfg, info, info_name, tech_name)
           + "<content>" + wac + "<procedure>" + common + prelim
           + "<mainProcedure>" + "".join(steps_x) + "</mainProcedure>"
           + "<closeRqmts><reqCondGroup><noConds/></reqCondGroup></closeRqmts></procedure></content></dmodule>\n")
    return cfg.file_name(info), xml


def _ipd_item(item: str) -> tuple[str, str | None]:
    m = re.match(r"^(-?\d+)([A-Z]?)$", item or "")
    if not m:
        return "000", None
    n = max(int(m.group(1)), 0)
    return f"{n:03d}" if n < 1000 else str(n), (m.group(2) or None)


def render_ipd(ipl: dict, cfg: PublicationConfig, tech_name: str, graphic_icn: str | None = None) -> tuple[str, str]:
    info, info_name = INFO["ipd"]
    sns = cfg.sns()
    fig = f"{int(cfg.figure_number):02d}" if str(cfg.figure_number).isdigit() else cfg.figure_number
    icn = graphic_icn or f"ICN-{cfg.model_ident_code}-{cfg.system_diff_code}-{cfg.system_code}{cfg.sub_system_code}" \
                         f"{cfg.sub_sub_system_code}{cfg.assy_code}-{cfg.item_location_code}-{cfg.enterprise_code or 'ZZZZZ'}-00001-A-001-01"
    out = [f"<figure id={quoteattr('fig-' + fig)}><title>{escape(tech_name)}</title><graphic{_a(infoEntityIdent=icn)}/></figure>"]
    for r in ipl["rows"]:
        if r.get("status") == "rejected" or not r.get("item"):
            continue
        item, variant = _ipd_item(r["item"])
        if r["depth"] == 0:
            item = "001"
        attrs = {**sns, "figureNumber": fig, "item": item, "itemVariant": variant,
                 "indenture": str(max(r["indent"], 1)), "id": f"csn-{fig}-{item}{(variant or '').lower()}"}
        qty = r.get("units_per_assy")
        qty_x = f"<quantityPerNextHigherAssy>{qty if qty is not None else 'AR'}</quantityPerNextHigherAssy>"
        descr = r["nomenclature"].capitalize() if r["nomenclature"].isupper() else r["nomenclature"]
        loc_inner = ("<notIllustrated/>" if not r.get("occ_ids") else "") + \
                    ("<attachStoreShipPart attachStoreShipPartCode=\"1\"/>" if r.get("attaching_for") else "")
        loc = f"<partLocationSegment>{loc_inner}</partLocationSegment>" if loc_inner else ""
        applic = (f"<applicabilitySegment><usableOnCodeAssy>{escape(r['effectivity'])}</usableOnCodeAssy></applicabilitySegment>"
                  if r.get("effectivity") else "")
        out.append(f"<catalogSeqNumber{_a(**attrs)}><itemSeqNumber itemSeqNumberValue=\"00A\">{qty_x}"
                   f"<partRef{_a(manufacturerCodeValue=r.get('cage') or cfg.enterprise_code, partNumberValue=r['part_number'])}/>"
                   f"<partSegment><itemIdentData><descrForPart>{escape(descr)}</descrForPart></itemIdentData></partSegment>"
                   f"{loc}{applic}</itemSeqNumber></catalogSeqNumber>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<dmodule xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xlink="http://www.w3.org/1999/xlink" '
           'xsi:noNamespaceSchemaLocation="http://www.s1000d.org/S1000D_4-2/xml_schema_flat/ipd.xsd">'
           + _ident_status(cfg, info, info_name, tech_name)
           + "<content><illustratedPartsCatalog>" + "".join(out) + "</illustratedPartsCatalog></content></dmodule>\n")
    return cfg.file_name(info), xml
