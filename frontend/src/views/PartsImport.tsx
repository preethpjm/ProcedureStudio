import { useEffect, useMemo, useState } from "react";
import { api, Json } from "../api";
import { useStudio } from "../state";

/* How the parts list file was read: which table, which column is which field, and how sure the reading is.
   The writer corrects it here; the mapping is kept for this product and this file layout. */

const FIELDS: [string, string, boolean][] = [
  ["part_number", "Part number", true], ["description", "Description", true], ["qty", "Quantity", true],
  ["level", "Level / indenture", false], ["manuf_code", "CAGE / manufacturer", false], ["unit_measure", "Unit", false],
  ["find_number", "Find / item number", false], ["seq_number", "Sequence number", false], ["equiv", "Equivalence (J/K)", false],
  ["comments", "Remarks", false], ["cad_reference", "3D / CAD reference", false], ["material_spec", "Material", false],
];
const clean = (h: string | null | undefined) => (h ?? "").replace(/\s+/g, " ").trim();

export function PartsImport() {
  const { run, editable, toast, touch } = useStudio();
  const [imp, setImp] = useState<Json | null>(null);
  const [table, setTable] = useState("");
  const [cols, setCols] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!run) return;
    api.partsImport(run.id).then((d) => {
      setImp(d);
      setTable(d.mapping?.table ?? d.chosen ?? "");
      setCols({ ...(d.mapping?.columns ?? {}) });
    }).catch((e) => toast(e.message, "bad"));
  }, [run?.id, run?.stages?.reconcile?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const t: Json = useMemo(() => {
    if (!imp) return null;
    if (table === "pdf:*") return imp.tables.find((x: Json) => x.id.startsWith("pdf:")) ?? null;
    return imp.tables.find((x: Json) => x.id === table) ?? null;
  }, [imp, table]);
  const headers: string[] = (t?.headers ?? []).filter((h: string) => clean(h));
  const how = (f: string) => {
    const m = t?.mapping?.[f];
    if (!cols[f]) return "missing";
    return m && clean(m.header) === clean(cols[f]) ? m.how : "set by you";
  };

  if (!imp) return <p className="fine">Reading the parts list…</p>;
  const save = async () => {
    if (!run) return;
    setBusy(true);
    try {
      const r = await api.confirmPartsImport(run.id, table, cols);
      toast(`${r.rows} parts-list lines read. Matching again…`);
      touch();
    } catch (e) { toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const idx = (h: string) => (t?.headers ?? []).findIndex((x: string) => clean(x) === clean(h));
  const shown = FIELDS.filter(([f]) => cols[f]);

  return (
    <section className="parts-import">
      <p className="lede-s">
        <b>{imp.file}</b>: {imp.rows ? `${imp.rows} lines read` : "not read yet"}, {imp.source === "confirmed" ? "with the column mapping you confirmed for this layout." : "with the columns Studio proposed."}
      </p>
      {imp.problems?.length > 0 && <div className={`callout ${imp.needs_review ? "warn" : "info"}`}>
        <b>{imp.needs_review ? "Check the columns before relying on the matching:" : "Notes"}</b>
        <ul>{imp.problems.map((p: string, i: number) => <li key={i}>{p}</li>)}</ul></div>}
      <div className="fields">
        <label className="wide"><span>Table</span>
          <select value={table} onChange={(e) => { setTable(e.target.value); const nt = imp.tables.find((x: Json) => x.id === e.target.value);
            if (nt) setCols(Object.fromEntries(Object.entries(nt.mapping).map(([f, m]: [string, Json]) => [f, m.header]))); }} disabled={!editable}>
            {imp.tables.some((x: Json) => x.id.startsWith("pdf:")) && <option value="pdf:*">Every page with this layout (PDF)</option>}
            {imp.tables.map((x: Json) => <option key={x.id} value={x.id}>{x.label} · {x.rows} lines</option>)}
          </select></label>
      </div>
      <table className="grid compact static map-grid">
        <thead><tr><th>Field</th><th>Column in the file</th><th>How it was found</th></tr></thead>
        <tbody>{FIELDS.map(([f, label, req]) => (
          <tr key={f}>
            <td>{label}{req && <span className="fine"> · required</span>}</td>
            <td><select value={cols[f] ?? ""} disabled={!editable} onChange={(e) => setCols({ ...cols, [f]: e.target.value })}>
              <option value="">Not in this file</option>
              {headers.map((h, i) => <option key={i} value={h}>{clean(h)}</option>)}
            </select></td>
            <td><span className={`tag ${how(f) === "exact" ? "ok" : how(f) === "missing" ? (req ? "bad" : "") : how(f) === "set by you" ? "info" : "warn"}`}>
              {how(f) === "exact" ? "known header" : how(f) === "guessed" ? `guessed${t?.mapping?.[f]?.why ? `: ${t.mapping[f].why}` : ""}` : how(f) === "missing" ? "not found" : "set by you"}</span></td>
          </tr>))}</tbody>
      </table>
      {t && shown.length > 0 && <>
        <h3>First lines, as they will be read</h3>
        <div className="table-wrap"><table className="grid compact static">
          <thead><tr>{shown.map(([f, l]) => <th key={f}>{l}</th>)}</tr></thead>
          <tbody>{(t.sample ?? []).map((row: string[], i: number) => <tr key={i}>{shown.map(([f]) => <td key={f}>{row[idx(cols[f])] ?? ""}</td>)}</tr>)}</tbody>
        </table></div>
      </>}
      {editable && <div className="actions">
        <button className="btn primary" disabled={busy || !cols.part_number} onClick={save}>{busy ? "Reading…" : "Use this mapping and match again"}</button>
        <span className="fine">Kept for this product: the next file with the same columns is read the same way.</span>
      </div>}
    </section>
  );
}
