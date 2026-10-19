import { useEffect, useMemo, useState } from "react";
import { Json } from "../api";
import { occsForPn, useArtifact, useStudio } from "../state";
import { PartsImport } from "./PartsImport";

/* 3D <-> MBOM reconciliation: what the stp2glb reconciler matched, how sure it is, and what is left over. */

const STATUS: Record<string, { label: string; cls: string }> = {
  matched: { label: "Exact", cls: "ok" },
  exact_match: { label: "Exact", cls: "ok" },
  normalized_match: { label: "Normalised", cls: "ok" },
  fuzzy_candidate: { label: "Fuzzy", cls: "warn" },
  supersession_match: { label: "Superseded", cls: "info" },
  variant_match: { label: "Variant", cls: "info" },
  fuzzy_match: { label: "Fuzzy", cls: "warn" },
  translated_candidate: { label: "Translated", cls: "warn" },
  ambiguous: { label: "Ambiguous", cls: "warn" },
  unmatched: { label: "No match", cls: "bad" },
};

export function MatchView() {
  const { data: rec } = useArtifact<Json>("reconciliation");
  const { setScene, nodes, pickHandler, run } = useStudio();
  const review = !!run?.stages?.reconcile?.summary?.parts_list_needs_review;
  const [tab, setTab] = useState<"matched" | "unmatched_step" | "unmatched_mbom" | "import">(review ? "import" : "matched");
  const [sel, setSel] = useState<number | null>(null);

  const rows: Json[] = tab === "import" ? [] : rec?.[tab] ?? [];
  const tint = useMemo(() => {
    const m = new Map<string, string>();
    for (const r of rec?.matched ?? []) for (const o of occsForPn(nodes, r.step_part_number)) m.set(o, (STATUS[r.status]?.cls ?? "ok") === "ok" ? "#8fb5a3" : "#d9a283");
    for (const r of rec?.unmatched_step ?? []) for (const o of occsForPn(nodes, r.step_part_number)) m.set(o, "#c46a3a");
    return m;
  }, [rec, nodes]);

  useEffect(() => {
    const r = sel != null ? rows[sel] : null;
    setScene({ selected: new Set(r ? occsForPn(nodes, r.step_part_number) : []), tint, dim: null, explode: undefined, hidden: undefined });
  }, [sel, rows, nodes, tint, setScene]);

  useEffect(() => {
    pickHandler.current = (id) => {
      if (!id) { setSel(null); return; }
      for (const t of ["matched", "unmatched_step"] as const) {
        const i = (rec?.[t] ?? []).findIndex((r: Json) => occsForPn(nodes, r.step_part_number).includes(id));
        if (i >= 0) { setTab(t); setSel(i); return; }
      }
    };
    return () => { pickHandler.current = null; };
  }, [rec, nodes, pickHandler]);

  if (!rec) {
    const failed = run?.stages?.reconcile?.status === "failed";
    return (
      <div className="view match">
        <header className="view-head"><h1>Match 3D to parts list</h1>
          {failed ? <p className="callout bad">{run?.stages?.reconcile?.error}</p> : <p className="fine">The parts list is matched after the 3D conversion.</p>}</header>
        {failed && <PartsImport />}
      </div>
    );
  }
  const s = rec.summary ?? {};
  const cur = sel != null ? rows[sel] : null;

  return (
    <div className="view match">
      <header className="view-head">
        <h1>Match 3D to parts list</h1>
        <p className="lede-s">{s.matched} of {s.matched + (s.unmatched_or_ambiguous_step ?? 0)} distinct 3D parts matched to the parts list. {s.unmatched_mbom_lines} parts-list lines have no 3D part; most are processes, specifications and items not modelled.</p>
        <div className="legend"><span className="sw" style={{ background: "#8fb5a3" }} />Matched <span className="sw" style={{ background: "#d9a283" }} />Matched, check <span className="sw" style={{ background: "#c46a3a" }} />Not matched</div>
      </header>
      <div className="tabs" role="tablist">
        {([["matched", `Matched (${rec.matched.length})`], ["unmatched_step", `3D only (${rec.unmatched_step.length})`], ["unmatched_mbom", `Parts list only (${rec.unmatched_mbom.length})`], ["import", review ? "Parts list import · check" : "Parts list import"]] as const).map(([k, l]) =>
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => { setTab(k); setSel(null); }}>{l}</button>)}
      </div>
      {tab === "import" ? <PartsImport /> : <div className="table-wrap">
        <table className="grid">
          <thead>{tab === "unmatched_mbom" ?
            <tr><th>Level</th><th>Part number</th><th>Description</th><th className="num">Qty</th><th>CAGE</th></tr> :
            <tr><th>3D part</th><th>3D name</th><th>Parts-list match</th><th>How</th><th className="num">Score</th><th className="num">Qty</th></tr>}
          </thead>
          <tbody>
            {rows.map((r: Json, i: number) => tab === "unmatched_mbom" ? (
              <tr key={i}><td>{r.level}</td><td className="pn">{r.part_number}</td><td>{r.description}</td><td className="num">{r.qty}</td><td>{r.manuf_code}</td></tr>
            ) : (
              <tr key={i} className={sel === i ? "sel" : ""} onClick={() => setSel(sel === i ? null : i)} tabIndex={0}
                onKeyDown={(e) => e.key === "Enter" && setSel(i)}>
                <td className="pn">{r.step_part_number}</td>
                <td>{r.step_name}</td>
                <td>{r.mbom_part_number ? <><span className="pn">{r.mbom_part_number}</span> <span className="fine">{r.mbom_description}</span></> : "—"}</td>
                <td><span className={`tag ${STATUS[r.status]?.cls ?? ""}`}>{STATUS[r.status]?.label ?? r.status}</span></td>
                <td className="num"><Confidence v={r.confidence} /></td>
                <td className="num">{r.step_qty}{r.mbom_qty != null && r.mbom_qty !== r.step_qty ? <span className="warn-t"> / {r.mbom_qty}</span> : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>}
      {cur && tab !== "unmatched_mbom" && tab !== "import" && (
        <aside className="detail">
          <h3><span className="pn">{cur.step_part_number}</span> {cur.step_name}</h3>
          {(cur.flags ?? []).map((f: string, i: number) => <p key={i} className="flag">{f}</p>)}
          {cur.candidates?.length > 0 && <>
            <h4>Other candidates</h4>
            <ul className="plain">{cur.candidates.slice(0, 5).map((c: Json, i: number) =>
              <li key={i}><span className="pn">{c.part_number ?? c.mbom_part_number}</span> {c.description ?? c.mbom_description} <Confidence v={c.score ?? c.confidence} /></li>)}</ul>
          </>}
          <p className="fine">Change a match in the parts list: open the row in Illustrated parts list and set its 3D occurrences.</p>
        </aside>
      )}
    </div>
  );
}

export function Confidence({ v }: { v: number | null | undefined }) {
  if (v == null) return <span className="fine">—</span>;
  const pct = Math.round(v * 100);
  return <span className={`conf ${v >= 0.85 ? "hi" : v >= 0.6 ? "mid" : "lo"}`} title={`${pct}% confidence`}><span className="conf-bar" style={{ width: `${pct}%` }} />{pct}%</span>;
}
