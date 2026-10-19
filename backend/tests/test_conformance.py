"""Schema validation, BREX, rule sets and stylesheets, on small documents (no sample product needed)."""
from pathlib import Path

import pytest

from studio.conformance.service import Conformance
from studio.publish.config import PublicationConfig

ROOT = Path(__file__).resolve().parents[2]

XSD = b"""<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="manual"><xs:complexType><xs:sequence>
    <xs:element name="title" type="xs:string"/>
    <xs:element name="step" maxOccurs="unbounded"><xs:complexType><xs:simpleContent><xs:extension base="xs:string">
      <xs:attribute name="id" type="xs:ID" use="required"/><xs:attribute name="ref" type="xs:IDREF"/>
    </xs:extension></xs:simpleContent></xs:complexType></xs:element>
  </xs:sequence><xs:attribute name="security" type="xs:string"/></xs:complexType></xs:element>
</xs:schema>"""

GOOD = b"""<?xml version="1.0"?>
<manual security="01">
  <title>Valve</title>
  <step id="s1">Remove the cap.</step>
  <step id="s2" ref="s1">Remove the screws; keep them.</step>
</manual>"""


@pytest.fixture()
def conf(tmp_path):
    c = Conformance(tmp_path / "conformance")
    src = tmp_path / "raw"
    src.mkdir()
    (src / "manual.xsd").write_bytes(XSD)
    prop = c.inspect_upload([("acme/manual.xsd", XSD)])
    assert prop["kind"] == "xsd" and prop["doc_types"][0]["root"] == "manual"
    prop.update(issue="1.0", standard="OEM", name="ACME manual")
    c.install_staged(prop["staging"], prop)
    return c


def test_valid_document_passes(conf):
    r = conf.check(GOOD, "good.xml")
    assert r["identification"]["package_id"] == "oem/1.0/oem"
    assert r["statuses"] == {"structural": "passed", "business_rules": "not-run"}
    assert r["passed"]


def test_schema_errors_are_explained_with_lines(conf):
    bad = GOOD.replace(b"<title>Valve</title>", b"").replace(b'ref="s1"', b'ref="s9"').replace(b'id="s2"', b'id="s1"')
    r = conf.check(bad, "bad.xml")
    assert r["statuses"]["structural"] == "failed" and not r["passed"]
    rules = {d["rule_id"] for d in r["diagnostics"]}
    assert "VAL-DUPLICATE-ID" in rules and "XSD-IDREF" in rules
    missing = next(d for d in r["diagnostics"] if "title" in d["message"])
    assert missing["line"] == 4 and missing.get("suggestion")       # reported at the step where <title> was expected


def test_not_well_formed(conf):
    r = conf.check(b"<manual><title>x</manual>", "broken.xml")
    assert r["stages"][1]["status"] == "failed" and r["diagnostics"][0]["rule_id"] == "XML-WF"


def test_yaml_rules(conf):
    conf.rules.install(b"""
id: acme
name: ACME rules
applies_to: {roots: [manual]}
rules:
  - id: AC-1
    context: //step
    forbid: "contains(., ';')"
    message: No semicolons.
  - id: AC-2
    context: /manual
    attribute: security
    allowed: ["02"]
  - id: AC-3
    severity: warning
    context: //step
    pattern: '^[A-Z].*\\.$'
""", "acme.yaml")
    r = conf.check(GOOD, "good.xml")
    found = {(d["rule_id"], d["severity"]) for d in r["diagnostics"]}
    assert ("AC-1", "error") in found and ("AC-2", "error") in found and ("AC-3", "warning") not in found
    assert r["statuses"]["business_rules"] == "failed"
    conf.rules.update("acme", enabled=False)
    assert conf.check(GOOD, "good.xml")["statuses"]["business_rules"] == "not-run"


@pytest.mark.parametrize("text, err", [
    (b"rules: []", "non-empty"),
    (b"rules: [{id: X, assert: 'true()'}]", "context"),
    (b"rules: [{id: X, context: '//a[', assert: 'true()'}]", "X"),
    (b"rules: [{id: X, context: '//a', assert: 'true()', forbid: 'false()'}]", "exactly one"),
])
def test_bad_rule_files_are_refused(conf, text, err):
    with pytest.raises(ValueError, match=err):
        conf.rules.install(text, "bad.yaml")


def test_schematron(conf):
    conf.rules.install(b"""<schema xmlns="http://purl.oclc.org/dsdl/schematron"><title>Sch</title><pattern>
      <rule context="step"><assert id="SCH-STEP" role="warning" test="string-length(.) &lt; 20">Short steps.</assert></rule>
      <rule context="manual"><report id="SCH-SEC" test="@security='01'">Unclassified manual.</report></rule>
    </pattern></schema>""", "sch.sch")
    r = conf.check(GOOD, "good.xml")
    d = {x["rule_id"]: x for x in r["diagnostics"]}
    assert d["SCH-STEP"]["severity"] == "warning" and d["SCH-STEP"]["line"] == 5
    assert d["SCH-SEC"]["severity"] == "error"


def test_brex_chain_and_rule_ids(conf):
    brex = (ROOT / "examples" / "brex" / "DMC-SLSBSELV-A-00-00-00-00A-022A-D_001-00_EN-US.XML").read_bytes()
    e = conf.brex.install(brex, "project.xml")
    assert e["parent_dmc"] == "S1000D-G-04-10-0301-00A-022A-D" and e["rules"] == 5
    chain, missing = conf.brex.chain(e["dmc"])
    assert [x["dmc"] for x in chain] == [e["dmc"]] and missing == "S1000D-G-04-10-0301-00A-022A-D"
    rules = conf.brex.compiled(e).rules
    assert [c.rule.rule_id for c in rules][:2] == ["SLS-001", "SLS-002"]


def test_view_and_transform_stylesheets(conf, tmp_path):
    html = conf.view(GOOD)
    assert html["stylesheet"] is None and "manual" in html["html"]           # no view fits: outline
    conf.styles.install(b"""<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
      <xsl:template match="/"><html><body><h1><xsl:value-of select="manual/title"/></h1></body></html></xsl:template>
    </xsl:stylesheet>""", "manual-view.xsl", "view", applies_to={"roots": ["manual"]})
    v = conf.view(GOOD)
    assert v["stylesheet"]["id"] == "manual-view" and "<h1>Valve</h1>" in v["html"]
    conf.styles.install(b"""<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
      <xsl:template match="@*|node()"><xsl:copy><xsl:apply-templates select="@*|node()"/></xsl:copy></xsl:template>
      <xsl:template match="@security"><xsl:attribute name="security">02</xsl:attribute></xsl:template>
    </xsl:stylesheet>""", "secure.xsl", "transform")
    out, used = conf.transform(GOOD, "OEM", "manual")
    assert [u["id"] for u in used] == ["secure"] and b'security="02"' in out


def test_stylesheets_cannot_write_files_or_reach_the_network(conf, tmp_path):
    target = tmp_path / "written.txt"
    evil = f"""<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:exsl="http://exslt.org/common"
      extension-element-prefixes="exsl"><xsl:template match="/"><exsl:document href="{target.as_uri()}">x</exsl:document><r/></xsl:template>
      </xsl:stylesheet>""".encode()
    conf.styles.install(evil, "evil.xsl", "transform")
    with pytest.raises(ValueError):
        conf.transform(GOOD, "OEM", "manual")
    assert not target.exists()
    net = b"""<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
      <xsl:template match="/"><r><xsl:copy-of select="document('http://example.com/x.xml')"/></r></xsl:template></xsl:stylesheet>"""
    conf.styles.remove("evil")
    conf.styles.install(net, "net.xsl", "transform")
    with pytest.raises(ValueError):
        conf.transform(GOOD, "OEM", "manual")


def test_builtin_views_cannot_be_removed(conf):
    with pytest.raises(ValueError):
        conf.styles.remove("builtin-s1000d-view")
    conf.styles.update("builtin-s1000d-view", enabled=False)
    assert next(s for s in conf.styles.list() if s["id"] == "builtin-s1000d-view")["enabled"] is False


def test_publication_brex_reference():
    c = PublicationConfig.from_dict({"brex_dmc": "DMC-SLSBSELV-A-00-00-00-00A-022A-D"})
    assert c.brex_dmcode() == {"modelIdentCode": "SLSBSELV", "systemDiffCode": "A", "systemCode": "00", "subSystemCode": "0",
                               "subSubSystemCode": "0", "assyCode": "00", "disassyCode": "00", "disassyCodeVariant": "A",
                               "infoCode": "022", "infoCodeVariant": "A", "itemLocationCode": "D"}
    with pytest.raises(ValueError):
        PublicationConfig.from_dict({"brex_dmc": "not a code"}).brex_dmcode()
