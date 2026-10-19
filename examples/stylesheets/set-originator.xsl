<?xml version="1.0" encoding="UTF-8"?>
<!-- Example transform (kind "transform"), applied to S1000D output before validation and publishing:
     copies everything unchanged and marks the data module as verified by a first review (tabtop).
     Use it as a starting point for house conventions or a customer's flavour of the schema. -->
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="xml" encoding="UTF-8"/>
  <xsl:template match="@*|node()"><xsl:copy><xsl:apply-templates select="@*|node()"/></xsl:copy></xsl:template>
  <xsl:template match="qualityAssurance/unverified">
    <firstVerification verificationType="tabtop"/>
  </xsl:template>
</xsl:stylesheet>
