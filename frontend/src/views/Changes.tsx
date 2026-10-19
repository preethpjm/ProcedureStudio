import { useEffect, useMemo, useState } from "react";
import { Json } from "../api";
import { plural, useArtifact, useStudio } from "../state";

/* What changed between the signed-off baseline and this run, which procedure steps it touches,
   and how much of the baseline text was carried forward unchanged. */

const TYPE: Record<string, string> = {
  part_number_changed: "Part number changed", added: "Part added", removed: "Part removed", moved: "Part moved",
  geometry_changed: "Geometry changed", quantity_changed: "Quantity changed", torque_changed: "Torque changed",
  nomenclature_changed: "Name changed", note_changed: "Drawing note changed", ipl_added: "Parts-list line added",
  ipl_removed: "Parts-list line removed", ipl_changed: "Parts-list line changed",
};
const typeLabel = (t: string) => TYPE[t] ?? t.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

export function ChangesView() {
  const { data: cs, error } = useArtifact<Json>("changeset");
  const { setScene, pickHandler, nodes, setView, setProcKind, project, run } = useStudio();
  const [sel, setSel] = useState<string | null>(null);
  const changes: Json[] = cs?.changes ?? [];

  const tint = useMemo(() => {
    const m = new Map<string, string>();
    changes.forEach((c) => (c.occ_ids_new ?? []).forEach((o: string) => m.set(o, "#6f95bd")));
    return m;
  }, [changes]);
  const dim = useMemo(() => new Set(changes.flatMap((c) => c.occ_ids_new ?? [])), [changes]);
  useEffect(() => {
    const c = changes.find((x) => x.id === sel);
    setScene({ selected: new Set(c?.occ_ids_new ?? []), tint, dim: dim.size ? dim : null, explode: undefined, hidden: undefined });
  }, [sel, changes, tint, dim, setScene]);
  useEffect(() => () => setScene({ dim: null }), [setScene]);
  useEffect(() => {
    pickHandler.current = (id) => setSel(changes.find((c) => c.occ_ids_new?.includes(id))?.id ?? null);
    return () => { pickHandler.current = null; };
  }, [changes, pickHandler]);

  if (error) return <div className="view"><h1>Changes from baseline</h1><p className="fine">This run has no comparison yet. It is made when the run has a signed-off baseline.</p></div>;
  if (!cs) return <div className="view"><p className="fine">Loading…</p></div>;
  const base = project?.run_list?.find((r) => r.id === run?.base_run);
  const ctx = cs.context ?? {};
  const baseName = base?.label ?? (ctx.base ? `${ctx.base.product}, ${ctx.base.label}` : "the baseline");
  const cf = cs.carry_forward ?? {};

  return (
    <div className="view changes">
      <header className="view-head">
        <h1>{ctx.kind === "variant" ? `Differences from ${baseName}` : ctx.kind === "modification" ? `${ctx.reference}: changes from ${baseName}` : `Changes from ${baseName}`}</h1>
        {(ctx.title || ctx.reason) && <p className="fine">{ctx.title}{ctx.title && ctx.reason ? " · " : ""}{ctx.reason ? `Reason: ${ctx.reason}` : ""}</p>}
        <p className="lede-s">{changes.length ? `${plural(changes.length, "change")} found in the 3D model, parts list and drawing. ${plural(cs.summary?.affected_steps ?? 0, "procedure step")} in ${plural(cs.summary?.affected_subtasks ?? 0, "subtask")} are affected.` : "No changes from the baseline: the procedures carry over as they are."}</p>
        <div className="carry">
          {(["disassembly", "assembly"] as const).map((k) => cf[k] && (
            <div key={k} className="carry-bar" title={`${cf[k].kept} steps kept word for word, ${cf[k].changed} regenerated`}>
              <span className="carry-label">{k === "disassembly" ? "Disassembly" : "Assembly"}</span>
              <span className="bar2"><span className="kept" style={{ flex: cf[k].kept }} /><span className="chg" style={{ flex: Math.max(cf[k].changed, 0.0001) }} /></span>
              <span className="fine">{cf[k].kept} kept, {cf[k].changed} regenerated</span>
            </div>
          ))}
        </div>
        {changes.length > 0 && <button className="btn" onClick={() => { setProcKind("modification"); setView("procedures"); }}>{ctx.kind === "variant" ? "Open the conversion procedure" : "Open the modification procedure"}</button>}
      </header>

      <ol className="change-list">
        {changes.map((c) => (
          <li key={c.id} className={sel === c.id ? "sel" : ""} onClick={() => setSel(sel === c.id ? null : c.id)}>
            <div className="chg-head">
              <span className="tag info">{typeLabel(c.type)}</span>
              <span className="chg-msg">{c.part_number_base || c.part_number_new ? (c.name ?? c.message) : c.message}{c.count > 1 ? ` (${c.count} places)` : ""}</span>
              <span className="fine">{c.source}</span>
            </div>
            {(c.part_number_base || c.part_number_new) && <p className="chg-pn"><span className="pn old">{c.part_number_base ?? "—"}</span> <span aria-hidden>→</span> <span className="pn">{c.part_number_new ?? "—"}</span>
              {c.geometry && c.source !== "IPL" && <span className="fine"> · {c.geometry === "same" ? "same geometry and position" : `geometry ${c.geometry}`}</span>}</p>}
            {c.ipl && <p className="fine">{c.ipl}</p>}
            {(c.flags ?? []).map((f: string, i: number) => <p key={i} className="flag">{f}</p>)}
            {c.affected_steps?.length > 0 && <>
              <h4>Steps that change</h4>
              <ul className="aff">{c.affected_steps.map((s: Json, i: number) => <li key={i}><span className="fine">{s.procedure}</span> {s.text}</li>)}</ul>
            </>}
            {nodes.length > 0 && !(c.occ_ids_new ?? []).length && c.source !== "IPL" && <p className="fine">Not in the new 3D model.</p>}
            {c.source === "IPL" && <p className="flag">The 3D model was not changed: it still shows {c.part_number_base ?? "the previous part"}. Update the 3D, or confirm the change applies to the parts list only.</p>}
          </li>
        ))}
      </ol>
    </div>
  );
}
