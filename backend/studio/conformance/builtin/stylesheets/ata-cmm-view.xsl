<?xml version="1.0" encoding="UTF-8"?>
<!-- Procedure Studio: reading view for ATA iSpec 2200 CMM XML (page blocks, tasks, subtasks, IPL). -->
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="html" encoding="UTF-8" indent="no"/>
  <xsl:template match="/">
    <html><head><meta charset="utf-8"/><title><xsl:value-of select="cmm/title"/></title>
      <style>
        body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:#18212b;margin:0;background:#fff}
        main{max-width:56rem;margin:0 auto;padding:1.5rem 2rem 3rem}
        header.doc{border-bottom:2px solid #18212b;padding-bottom:.6rem;margin-bottom:1rem}
        .ata{font:600 12px/1.4 ui-monospace,Menlo,monospace;color:#4a5562}
        h1{font-size:1.4rem;margin:.25rem 0 0} h1 small{display:block;font-size:1rem;font-weight:500;color:#4a5562}
        h2{font-size:1.15rem;margin:1.6rem 0 .3rem;border-bottom:1px solid #18212b}
        h3{font-size:1rem;margin:1rem 0 .3rem} h4{font-size:.92rem;margin:.9rem 0 .2rem}
        .key{font:11px ui-monospace,Menlo,monospace;color:#6f7a86;font-weight:400;margin-left:.5rem}
        ol{padding-left:1.6rem} li{margin:.2rem 0}
        .wc{border:1.5px solid #18212b;padding:.5rem .8rem;margin:.6rem 0;max-width:42rem}
        .wc b{display:block;text-align:center;letter-spacing:.08em;font-size:12px} .wc.caution{border-style:dashed}
        table{border-collapse:collapse;width:100%;font-size:13px} th,td{border:1px solid #c9cfd6;padding:.25rem .5rem;text-align:left;vertical-align:top}
        th{background:#f2f4f6} td.num{text-align:right}
        .meta{display:flex;flex-wrap:wrap;gap:.2rem 1.2rem;font-size:12px;color:#6f7a86;margin-top:.4rem}
      </style></head>
      <body><main><xsl:apply-templates select="cmm"/></main></body></html>
  </xsl:template>
  <xsl:template match="cmm">
    <header class="doc">
      <div class="ata">ATA <xsl:value-of select="concat(@chapnbr,'-',@sectnbr,'-',@subjnbr)"/> · <xsl:value-of select="@docnbr"/></div>
      <h1><xsl:value-of select="partinfo/cmpnom"/><small><xsl:value-of select="title"/></small></h1>
      <div class="meta"><span>Part number <xsl:value-of select="partinfo/mfrpnr/pnr"/> (<xsl:value-of select="partinfo/mfrpnr/mfr"/>)</span>
        <span>Revision date <xsl:value-of select="@revdate"/></span><xsl:if test="@prelim='1'"><span>Preliminary</span></xsl:if></div>
    </header>
    <xsl:apply-templates select="pgblk|ipl"/>
  </xsl:template>
  <xsl:template match="pgblk">
    <h2><xsl:value-of select="title"/><span class="key">page block <xsl:value-of select="@pgblknbr"/></span></h2>
    <xsl:apply-templates select="task"/>
  </xsl:template>
  <xsl:template match="task"><h3><xsl:value-of select="title"/><span class="key"><xsl:value-of select="@key"/></span></h3><xsl:apply-templates select="topic/subtask|subtask"/></xsl:template>
  <xsl:template match="subtask">
    <h4><xsl:value-of select="title"/><span class="key"><xsl:value-of select="@key"/></span></h4>
    <xsl:for-each select="warning|caution"><div class="wc {local-name()}"><b><xsl:choose><xsl:when test="self::warning">WARNING</xsl:when><xsl:otherwise>CAUTION</xsl:otherwise></xsl:choose></b>
      <xsl:for-each select="para"><p><xsl:value-of select="."/></p></xsl:for-each></div></xsl:for-each>
    <xsl:apply-templates select="prclist1"/>
  </xsl:template>
  <xsl:template match="prclist1|prclist2|prclist3"><ol type="{substring('Aa1', count(ancestor::*[starts-with(local-name(),'prclist')]) + 1, 1)}"><xsl:apply-templates select="*"/></ol></xsl:template>
  <xsl:template match="prcitem1|prcitem2|prcitem3"><li><xsl:apply-templates select="prcitem/*"/></li></xsl:template>
  <xsl:template match="para"><p><xsl:apply-templates/></p></xsl:template>
  <xsl:template match="con"><xsl:value-of select="conname"/> (<xsl:value-of select="connbr"/>)</xsl:template>
  <xsl:template match="ted"><xsl:value-of select="toolname"/><xsl:if test="toolnbr"> (<xsl:value-of select="toolnbr"/>)</xsl:if></xsl:template>
  <xsl:template match="ipl">
    <h2><xsl:value-of select="title"/></h2>
    <xsl:for-each select="dplist/figure">
      <h3>Figure <xsl:value-of select="position()"/>. <xsl:value-of select="title"/></h3>
      <table><tr><th>Item</th><th>Part number</th><th>Nomenclature</th><th>Eff.</th><th class="num">Units</th></tr>
        <xsl:for-each select="prtlist/itemdata">
          <tr><td><xsl:value-of select="@itemnbr"/></td><td><xsl:value-of select="pnr"/></td>
            <td><span style="padding-left:{@indent * 1.1}em"><xsl:if test="@attach='1'">· </xsl:if>
              <xsl:value-of select="iplnom/nom/kwd"/><xsl:if test="iplnom/nom/adt">, <xsl:value-of select="iplnom/nom/adt"/></xsl:if>
              <xsl:for-each select="iplnom/msc"> <xsl:value-of select="."/></xsl:for-each></span></td>
            <td><xsl:value-of select="effcode"/></td><td class="num"><xsl:value-of select="upa"/></td></tr>
        </xsl:for-each>
      </table>
    </xsl:for-each>
  </xsl:template>
</xsl:stylesheet>
