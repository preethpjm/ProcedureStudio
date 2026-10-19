<?xml version="1.0" encoding="UTF-8"?>
<!-- Example Schematron for ATA iSpec 2200 CMM XML. @id is the rule id, @role the severity. -->
<schema xmlns="http://purl.oclc.org/dsdl/schematron" queryBinding="xslt">
  <title>CMM house checks</title>
  <pattern id="cmm-structure">
    <rule context="cmm">
      <assert id="CMM-001" role="error" test="pgblk[@pgblknbr='3000'] and pgblk[@pgblknbr='7000']">A CMM with procedures has both DISASSEMBLY (3000) and ASSEMBLY (7000) page blocks.</assert>
      <assert id="CMM-002" role="error" test="string-length(@docnbr) &gt; 0">The CMM has a document number (docnbr).</assert>
    </rule>
    <rule context="subtask">
      <assert id="CMM-010" role="error" test="normalize-space(title) != ''">Every subtask has a title.</assert>
      <report id="CMM-011" role="warning" test="count(prclist1/prcitem1) &gt; 15">This subtask has more than 15 steps; consider splitting it.</report>
    </rule>
    <rule context="itemdata">
      <assert id="CMM-020" role="error" test="normalize-space(pnr) != ''">Every IPL item has a part number.</assert>
      <assert id="CMM-021" role="warning" test="upa = 'RF' or number(upa) = number(upa)">Units per assembly is a number or RF.</assert>
    </rule>
  </pattern>
</schema>
