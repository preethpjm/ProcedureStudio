<?xml version="1.0" encoding="UTF-8"?>
<!-- Procedure Studio: reading view for S1000D data modules (procedures 520-720, IPD 941, others generically).
     A review rendering, not a production print stylesheet. -->
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="html" encoding="UTF-8" indent="no"/>

  <xsl:template match="/">
    <html><head><meta charset="utf-8"/>
      <title><xsl:value-of select="//dmTitle/techName"/> - <xsl:value-of select="//dmTitle/infoName"/></title>
      <style>
        body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:#18212b;margin:0;background:#fff}
        main{max-width:56rem;margin:0 auto;padding:1.5rem 2rem 3rem}
        header.dm{border-bottom:2px solid #18212b;padding-bottom:.6rem;margin-bottom:1rem}
        .dmc{font:600 12px/1.4 ui-monospace,Menlo,monospace;color:#4a5562;letter-spacing:.02em}
        h1{font-size:1.45rem;margin:.25rem 0 0}
        h1 small{display:block;font-size:1rem;font-weight:500;color:#4a5562}
        .meta{display:flex;flex-wrap:wrap;gap:.2rem 1.2rem;font-size:12px;color:#6f7a86;margin-top:.4rem}
        h2{font-size:1.05rem;margin:1.4rem 0 .4rem;border-bottom:1px solid #c9cfd6;padding-bottom:.2rem}
        h3{font-size:.95rem;margin:1rem 0 .3rem}
        table{border-collapse:collapse;width:100%;font-size:13px;margin:.3rem 0}
        th,td{border:1px solid #c9cfd6;padding:.25rem .5rem;text-align:left;vertical-align:top}
        th{background:#f2f4f6;font-weight:600}
        td.num{text-align:right;font-variant-numeric:tabular-nums}
        .wc{border:1.5px solid #18212b;padding:.5rem .8rem;margin:.6rem 0;max-width:42rem}
        .wc b{display:block;text-align:center;letter-spacing:.08em;font-size:12px}
        .wc.caution{border-style:dashed}
        ol.steps{padding-left:1.6rem;margin:.2rem 0}
        ol.steps li{margin:.25rem 0}
        .none{color:#6f7a86}
        .att{color:#4a5562}
      </style></head>
      <body><main><xsl:apply-templates select="dmodule"/></main></body></html>
  </xsl:template>

  <xsl:template match="dmodule">
    <xsl:variable name="c" select="identAndStatusSection/dmAddress/dmIdent/dmCode"/>
    <header class="dm">
      <div class="dmc">DMC-<xsl:value-of select="concat($c/@modelIdentCode,'-',$c/@systemDiffCode,'-',$c/@systemCode,'-',$c/@subSystemCode,$c/@subSubSystemCode,'-',$c/@assyCode,'-',$c/@disassyCode,$c/@disassyCodeVariant,'-',$c/@infoCode,$c/@infoCodeVariant,'-',$c/@itemLocationCode)"/></div>
      <h1><xsl:value-of select="identAndStatusSection/dmAddress/dmAddressItems/dmTitle/techName"/>
        <small><xsl:value-of select="identAndStatusSection/dmAddress/dmAddressItems/dmTitle/infoName"/></small></h1>
      <div class="meta">
        <xsl:variable name="i" select="identAndStatusSection/dmAddress/dmIdent/issueInfo"/>
        <xsl:variable name="d" select="identAndStatusSection/dmAddress/dmAddressItems/issueDate"/>
        <span>Issue <xsl:value-of select="$i/@issueNumber"/>-<xsl:value-of select="$i/@inWork"/></span>
        <span><xsl:value-of select="concat($d/@year,'-',$d/@month,'-',$d/@day)"/></span>
        <span>Applicability: <xsl:value-of select="identAndStatusSection/dmStatus/applic/displayText"/></span>
        <span><xsl:value-of select="identAndStatusSection/dmStatus/responsiblePartnerCompany/enterpriseName"/></span>
      </div>
    </header>
    <xsl:apply-templates select="content/*"/>
  </xsl:template>

  <!-- ===================== procedures -->
  <xsl:template match="warningsAndCautions"/>
  <xsl:template match="procedure">
    <xsl:apply-templates select="commonInfo"/>
    <h2>Preliminary requirements</h2>
    <xsl:apply-templates select="preliminaryRqmts/*"/>
    <h2>Procedure</h2>
    <xsl:apply-templates select="mainProcedure"/>
    <h2>Requirements after job completion</h2>
    <xsl:choose>
      <xsl:when test="closeRqmts/reqCondGroup/noConds"><p class="none">None</p></xsl:when>
      <xsl:otherwise><xsl:apply-templates select="closeRqmts/*"/></xsl:otherwise>
    </xsl:choose>
  </xsl:template>
  <xsl:template match="commonInfo">
    <h2><xsl:value-of select="title"/></h2>
    <xsl:for-each select="para"><p><xsl:apply-templates/></p></xsl:for-each>
  </xsl:template>
  <xsl:template match="reqCondGroup"><h3>Required conditions</h3><xsl:if test="noConds"><p class="none">None</p></xsl:if></xsl:template>
  <xsl:template match="reqPersons">
    <h3>Required persons</h3>
    <table><tr><th>Category</th><th class="num">Number</th></tr>
      <xsl:for-each select="personnel"><tr><td><xsl:value-of select="personCategory/@personCategoryCode"/></td><td class="num"><xsl:value-of select="@numRequired"/></td></tr></xsl:for-each>
    </table>
  </xsl:template>
  <xsl:template match="reqSupportEquips|reqSupplies|reqSpares">
    <h3><xsl:choose><xsl:when test="self::reqSupportEquips">Support equipment</xsl:when><xsl:when test="self::reqSupplies">Consumables and supplies</xsl:when><xsl:otherwise>Spares</xsl:otherwise></xsl:choose></h3>
    <xsl:choose>
      <xsl:when test="noSupportEquips|noSupplies|noSpares"><p class="none">None</p></xsl:when>
      <xsl:otherwise>
        <table><tr><th>Name</th><th>Identification</th><th class="num">Quantity</th></tr>
          <xsl:for-each select="*/supportEquipDescr|*/supplyDescr|*/spareDescr">
            <tr><td><xsl:value-of select="name"/></td>
              <td><xsl:value-of select="identNumber/partAndSerialNumber/partNumber"/><xsl:if test="identNumber/manufacturerCode"> (<xsl:value-of select="identNumber/manufacturerCode"/>)</xsl:if></td>
              <td class="num"><xsl:value-of select="reqQuantity"/></td></tr>
          </xsl:for-each>
        </table>
      </xsl:otherwise>
    </xsl:choose>
  </xsl:template>
  <xsl:template match="reqSafety">
    <h3>Safety conditions</h3>
    <xsl:for-each select=".//warning|.//caution"><xsl:call-template name="wc"/></xsl:for-each>
  </xsl:template>
  <xsl:template name="wc">
    <div class="wc {local-name()}"><b><xsl:choose><xsl:when test="self::warning">WARNING</xsl:when><xsl:otherwise>CAUTION</xsl:otherwise></xsl:choose></b>
      <xsl:for-each select="warningAndCautionPara"><p><xsl:value-of select="."/></p></xsl:for-each></div>
  </xsl:template>
  <xsl:template match="mainProcedure"><ol class="steps"><xsl:apply-templates select="proceduralStep"/></ol></xsl:template>
  <xsl:template match="proceduralStep">
    <li>
      <xsl:for-each select="id(@warningRefs)|id(@cautionRefs)"><xsl:call-template name="wc"/></xsl:for-each>
      <xsl:if test="title"><h3><xsl:value-of select="title"/></h3></xsl:if>
      <xsl:for-each select="para"><p><xsl:apply-templates/></p></xsl:for-each>
      <xsl:if test="proceduralStep"><ol class="steps"><xsl:apply-templates select="proceduralStep"/></ol></xsl:if>
    </li>
  </xsl:template>

  <!-- ===================== IPD -->
  <xsl:template match="illustratedPartsCatalog">
    <xsl:for-each select="figure"><h2>Figure: <xsl:value-of select="title"/></h2></xsl:for-each>
    <table>
      <tr><th>Item</th><th>Part number</th><th>CAGE</th><th>Nomenclature</th><th class="num">Units</th><th>Eff.</th></tr>
      <xsl:for-each select="catalogSeqNumber/itemSeqNumber">
        <tr>
          <xsl:if test="partLocationSegment/attachStoreShipPart"><xsl:attribute name="class">att</xsl:attribute></xsl:if>
          <td><xsl:value-of select="concat(number(../@item), substring(@itemSeqNumberValue, 3))"/></td>
          <td><xsl:value-of select="partRef/@partNumberValue"/></td>
          <td><xsl:value-of select="partRef/@manufacturerCodeValue"/></td>
          <td><span style="padding-left:{(../@indenture - 1) * 1.1}em">
            <xsl:if test="partLocationSegment/attachStoreShipPart">· </xsl:if>
            <xsl:value-of select="partSegment/itemIdentData/descrForPart"/></span></td>
          <td class="num"><xsl:value-of select="quantityPerNextHigherAssy"/></td>
          <td><xsl:value-of select="applicabilitySegment/usableOnCodeAssy"/></td>
        </tr>
      </xsl:for-each>
    </table>
  </xsl:template>

  <!-- ===================== anything else -->
  <xsl:template match="content/*" priority="-1">
    <h2><xsl:value-of select="local-name()"/></h2>
    <xsl:for-each select=".//para|.//title"><p><xsl:value-of select="."/></p></xsl:for-each>
  </xsl:template>
</xsl:stylesheet>
