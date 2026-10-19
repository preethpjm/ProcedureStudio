import { useEffect, useMemo, useRef, useState } from "react";
import { api, Json } from "../api";
import { plural, useArtifact, useStudio } from "../state";
import { Confidence } from "./Match";

/* The proposed IPL, row by row against the 3D model. Writers accept rows, correct fields, and link rows to
   3D occurrences by picking in the viewer. Edits go to the backend, which re-runs the stages downstream. */

const ROLES = ["part", "housing", "lru", "fastener", "washer", "nut", "insert", "lockwire", "seal", "backup_ring", "press_plug",
  "plate", "fitting", "cap", "spring", "bush", "closure", "moving", "marker", "process"];
const ROLE_LABEL: Record<string, string> = { lru: "Unit", backup_ring: "Backup ring", press_plug: "Press-fit plug", lockwire: "Lockwire / cable" };
const roleLabel = (r: string) => ROLE_LABEL[r] ?? r.charAt(0).toUpperCase() + r.slice(1).replace(/_/g, " ");

type Filter = "all" | "flagged" | "no3d" | "open";

export function IplView() {
  const { data: ipl } = useArtifact<Json>("ipl");
  const { run, setScene, pickHandler, editable, toast, touch } = useStudio();
  const [filter, setFilter] = useState<Filter>("all");
  const [selId, setSelId] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [picking, setPicking] = useState<Set<string> | null>(null);   // draft 3D link while picking
  const rowRefs = useRef<Record<string, HTMLTableRowElement | null>>({});

  const { data: model } = useArtifact<Json>("product_model");
  const parentOf = useMemo(() => new Map<string, string>((model?.occurrences ?? []).filter((o: Json) => o.parent).map((o: Json) => [o.occ_id, o.parent])), [model]);
  const rows: Json[] = ipl?.rows ?? [];
  const byOcc = useMemo(() => {
    const m = new Map<string, Json>();
    rows.forEach((r) => r.occ_ids?.forEach((o: string) => { if (!m.has(o)) m.set(o, r); }));
    return m;
  }, [rows]);
  const shown = rows.filter((r) => {
    if (filter === "flagged" && !r.flags?.length) return false;
    if (filter === "no3d" && r.occ_ids?.length) return false;
    if (filter === "open" && r.status === "accepted") return false;
    if (q) {
      const t = q.toLowerCase();
      return [r.part_number, r.nomenclature, r.item, r.step_name].some((x) => x && String(x).toLowerCase().includes(t));
    }
    return true;
  });
  const sel = rows.find((r) => r.id === selId) ?? null;

  // viewer: selected row in chromate; flagged rows rust; accepted green; other mapped parts neutral
  const tint = useMemo(() => {
    const m = new Map<string, string>();
    rows.forEach((r) => r.occ_ids?.forEach((o: string) =>
      m.set(o, r.flags?.length ? "#d39a77" : r.status === "accepted" || r.status === "edited" ? "#9cbdae" : "#aab3bd")));
    (ipl?.unmatched_3d ?? []).forEach((u: Json) => u.occ_ids?.forEach((o: string) => m.set(o, "#7f9fc0")));
    return m;
  }, [rows, ipl]);
  useEffect(() => {
    const chosen = picking ?? new Set<string>(sel?.occ_ids ?? []);
    // ghost the rest when a selected part sits inside the housing; never while picking (everything must stay clickable)
    setScene({ selected: chosen, tint, dim: !picking && chosen.size ? chosen : null, explode: undefined, hidden: undefined });
  }, [sel, tint, picking, setScene]);

  useEffect(() => {
    pickHandler.current = (id) => {
      if (picking) {
        if (!id) return;
        setPicking((p) => { const n = new Set(p); if (n.has(id)) n.delete(id); else n.add(id); return n; });
        return;
      }
      // a body or phase of a part (or a part inside a sub-assembly not in the list) selects the nearest listed parent
      let at: string | undefined = id ?? undefined;
      while (at && !byOcc.has(at)) at = parentOf.get(at);
      const r = at ? byOcc.get(at) : null;
      setSelId(r?.id ?? null);
      if (r) { setFilter("all"); setQ(""); setTimeout(() => rowRefs.current[r.id]?.scrollIntoView({ block: "nearest" }), 0); }
      else if (id) toast(`${id} is not in the parts list yet. Select a row and link it with “Pick in 3D”.`);
    };
    return () => { pickHandler.current = null; };
  }, [byOcc, parentOf, picking, pickHandler, toast]);

  // keyboard: up/down through the visible rows, A to accept
  const onKey = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).tagName === "INPUT" || (e.target as HTMLElement).tagName === "SELECT") return;
    const i = shown.findIndex((r) => r.id === selId);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = shown[Math.max(0, Math.min(shown.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
      if (n) { setSelId(n.id); rowRefs.current[n.id]?.scrollIntoView({ block: "nearest" }); }
    } else if (e.key.toLowerCase() === "a" && sel && editable) { accept([sel.id]); }
  };

  const accept = async (ids: string[]) => {
    if (!run) return;
    try { const r = await api.acceptRows(run.id, ids); toast(ids.length ? "Row accepted" : `${plural(r.accepted, "row")} accepted`); touch(); }
    catch (e) { toast((e as Error).message, "bad"); }
  };
  const save = async (fields: Json) => {
    if (!run || !sel) return;
    try {
      const r = await api.editRow(run.id, sel.id, fields);
      toast(r.recompute ? "Saved. The procedures are being updated." : "Saved");
      touch();
    } catch (e) { toast((e as Error).message, "bad"); }
  };

  if (!ipl) return <div className="view"><p className="fine">Loading the parts list…</p></div>;
  const s = ipl.summary ?? {};
  const open = rows.filter((r) => r.status === "proposed").length;
  const flagged = rows.filter((r) => r.flags?.length).length;

  return (
    <div className="view ipl" onKeyDown={onKey}>
      <header className="view-head">
        <h1>Illustrated parts list <span className="sub">Figure {ipl.figure?.number} · {ipl.figure?.title}</span></h1>
        <p className="lede-s">{s.rows} rows, {s.with_3d} linked to the 3D model. Items are numbered in disassembly order, with attaching parts after the item they attach.
          {open ? ` ${plural(open, "row")} still to review.` : " Every row is reviewed."}</p>
        <div className="toolbar">
          <div className="seg" role="radiogroup" aria-label="Show">
            {([["all", `All ${rows.length}`], ["flagged", `Flagged ${flagged}`], ["no3d", "Not in 3D"], ["open", `To review ${open}`]] as const).map(([k, l]) =>
              <button key={k} role="radio" aria-checked={filter === k} className={filter === k ? "on" : ""} onClick={() => setFilter(k)}>{l}</button>)}
          </div>
          <input className="search" type="search" placeholder="Find part number or name" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Find in the parts list" />
          {editable && <button className="btn" onClick={() => accept([])} disabled={!rows.some((r) => r.status === "proposed" && !r.flags?.length)}
            title="Accept every row that has no flags">Accept unflagged rows</button>}
        </div>
      </header>

      <div className="table-wrap" tabIndex={0} aria-label="Parts list rows">
        <table className="grid ipl-grid">
          <thead><tr>
            <th className="num">Item</th><th>Part number</th><th>Nomenclature</th><th className="num">Units</th>
            <th>Torque</th><th>Status</th>
          </tr></thead>
          <tbody>
            {shown.map((r) => (
              <tr key={r.id} ref={(el) => { rowRefs.current[r.id] = el; }}
                className={[r.id === selId && "sel", r.flags?.length && "flagged", r.attaching && "att", !r.occ_ids?.length && "no3d"].filter(Boolean).join(" ")}
                onClick={() => { setSelId(r.id === selId ? null : r.id); setPicking(null); }}>
                <td className="num item">{r.item}{r.effectivity && <span className="eff" title={`Effectivity ${r.effectivity}`}>{r.effectivity}</span>}</td>
                <td className="pn">{r.part_number}</td>
                <td className="nom" title={roleLabel(r.role)} style={{ paddingLeft: `${0.6 + Math.max(0, r.indent - 1) * 0.9}rem` }}>
                  {r.attaching && <span className="att-mark" title="Attaching part">Attaching</span>}
                  {r.nomenclature}
                  {!r.occ_ids?.length && r.depth > 0 && <span className="no3d-mark" title="Not linked to the 3D model">no 3D</span>}
                </td>
                <td className="num">{r.units_per_assy ?? "RF"}</td>
                <td className="torque">{r.torque ? `${fmt(r.torque.nominal_nm)} ± ${fmt(r.torque.tolerance_nm)} N.m` : ""}</td>
                <td className="status-cell"><StatusTag r={r} /> <Confidence v={r.confidence} /></td>
              </tr>
            ))}
            {!shown.length && <tr><td colSpan={6} className="empty-row">No rows match.</td></tr>}
          </tbody>
        </table>
      </div>

      {sel && <RowDetail key={sel.id + (sel.status ?? "")} row={sel} rows={rows} editable={editable} picking={picking}
        onPick={() => setPicking(new Set(sel.occ_ids ?? []))}
        onPickDone={(keep) => { if (keep && picking) save({ occ_ids: [...picking] }); setPicking(null); }}
        onSave={save} onAccept={() => accept([sel.id])} onClose={() => setSelId(null)} />}
    </div>
  );
}

const fmt = (n: number | null | undefined) => n == null ? "" : String(Math.round(n * 100) / 100);

function StatusTag({ r }: { r: Json }) {
  if (r.status === "accepted") return <span className="tag ok">Accepted</span>;
  if (r.status === "edited") return <span className="tag ok">Edited</span>;
  if (r.flags?.length) return <span className="tag warn" title={r.flags.join("\n")}>{plural(r.flags.length, "flag")}</span>;
  return <span className="tag">Proposed</span>;
}

function RowDetail({ row, rows, editable, picking, onPick, onPickDone, onSave, onAccept, onClose }: {
  row: Json; rows: Json[]; editable: boolean; picking: Set<string> | null;
  onPick: () => void; onPickDone: (keep: boolean) => void; onSave: (f: Json) => void; onAccept: () => void; onClose: () => void;
}) {
  const [f, setF] = useState({
    item: row.item ?? "", nomenclature: row.nomenclature ?? "", units_per_assy: row.units_per_assy ?? "", effectivity: row.effectivity ?? "",
    role: row.role, nominal: row.torque?.nominal_nm ?? "", tol: row.torque?.tolerance_nm ?? "", notes: (row.notes ?? []).join("\n"),
  });
  const changed: Json = {};
  if (f.item !== (row.item ?? "")) changed.item = f.item;
  if (f.nomenclature !== (row.nomenclature ?? "")) changed.nomenclature = f.nomenclature;
  if (String(f.units_per_assy) !== String(row.units_per_assy ?? "")) changed.units_per_assy = f.units_per_assy === "" ? null : Number(f.units_per_assy);
  if (f.effectivity !== (row.effectivity ?? "")) changed.effectivity = f.effectivity || null;
  if (f.role !== row.role) changed.role = f.role;
  if (String(f.nominal) !== String(row.torque?.nominal_nm ?? "") || String(f.tol) !== String(row.torque?.tolerance_nm ?? "")) {
    const n = parseFloat(String(f.nominal).replace(",", ".")), t = parseFloat(String(f.tol).replace(",", ".")) || 0;
    changed.torque = isNaN(n) ? null : { nominal_nm: n, tolerance_nm: t, min_nm: +(n - t).toFixed(2), max_nm: +(n + t).toFixed(2), evidence: [{ text: "entered by the writer" }], confidence: 1 };
  }
  if (f.notes !== (row.notes ?? []).join("\n")) changed.notes = f.notes.split("\n").map((x: string) => x.trim()).filter(Boolean);
  const dirty = Object.keys(changed).length > 0;
  const parent = rows.find((r) => r.id === row.parent_row);
  const att = rows.find((r) => r.id === row.attaching_for);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) => setF({ ...f, [k]: e.target.value });

  return (
    <aside className="detail row-detail" aria-label={`Row ${row.item}`}>
      <div className="detail-head">
        <h3><span className="item-no">{row.item}</span> <span className="pn">{row.part_number}</span> {row.nomenclature}</h3>
        <button className="link" onClick={onClose} aria-label="Close">Close</button>
      </div>
      {row.flags?.map((x: string, i: number) => <p key={i} className="flag">{x}</p>)}
      <div className="detail-cols">
        <fieldset disabled={!editable}>
          <div className="fields">
            <label><span>Item</span><input value={f.item} onChange={set("item")} /></label>
            <label className="wide"><span>Nomenclature</span><input value={f.nomenclature} onChange={set("nomenclature")} /></label>
            <label><span>Units per assy</span><input inputMode="numeric" value={f.units_per_assy} onChange={set("units_per_assy")} /></label>
            <label><span>Effectivity</span><input value={f.effectivity} onChange={set("effectivity")} placeholder="—" /></label>
            <label><span>Role</span><select value={f.role} onChange={set("role")}>{ROLES.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}</select></label>
            <label><span>Torque, N.m</span><span className="pair"><input inputMode="decimal" value={f.nominal} onChange={set("nominal")} placeholder="nominal" aria-label="Nominal torque" />±<input inputMode="decimal" value={f.tol} onChange={set("tol")} placeholder="tol." aria-label="Torque tolerance" /></span></label>
            <label className="wide"><span>Notes for the procedure</span><textarea rows={2} value={f.notes} onChange={set("notes")} /></label>
          </div>
          <div className="actions">
            <button className="btn primary" disabled={!dirty} onClick={() => onSave(changed)}>Save changes</button>
            {row.status === "proposed" && <button className="btn" disabled={dirty} onClick={onAccept} title="Accept this row as proposed (A)">Accept row</button>}
            {!picking ? <button className="btn quiet" onClick={onPick} title="Choose the 3D parts for this row by clicking them in the model">Pick in 3D</button> :
              <span className="picking">Click parts in the model: {plural(picking.size, "part")} chosen.
                <button className="btn primary" onClick={() => onPickDone(true)}>Link these parts</button>
                <button className="link" onClick={() => onPickDone(false)}>Cancel</button></span>}
          </div>
        </fieldset>
        <dl className="facts">
          <dt>3D</dt><dd>{row.occ_ids?.length ? plural(row.occ_ids.length, "occurrence") : "Not linked"}{row.step_name ? ` · ${row.step_name}` : ""}</dd>
          {row.step_description && <><dt>3D description</dt><dd>{row.step_description}</dd></>}
          {parent && <><dt>Next higher</dt><dd>{parent.item} {parent.nomenclature}</dd></>}
          {att && <><dt>Attaches</dt><dd>{att.item} {att.nomenclature}</dd></>}
          {row.alternate_of && <><dt>Alternate of</dt><dd>{rows.find((r) => r.id === row.alternate_of)?.item ?? row.alternate_of}</dd></>}
          {row.disassembly_step && <><dt>Removed at</dt><dd>Step {row.disassembly_step} of the sequence</dd></>}
          {row.torque?.evidence?.length > 0 && <><dt>Torque from</dt><dd>{row.torque.evidence.map((e: Json) => e.page ? `drawing p.${e.page}: ${e.text}` : e.text).join("; ")}</dd></>}
          {row.mbom_comments && <><dt>Parts-list remark</dt><dd>{row.mbom_comments}</dd></>}
          {row.processes?.length > 0 && <><dt>Processes</dt><dd className="procs">{row.processes.map((x: Json, i: number) =>
            <span key={i}><b>{typeof x === "string" ? x : x.spec}</b>{typeof x === "object" && x.description ? ` ${x.description.toLowerCase()}` : ""}</span>)}</dd></>}
          <dt>Sources</dt><dd className="sources">{Object.entries(row.sources ?? {}).map(([k, v]) => <span key={k}><b>{k.replace(/_/g, " ")}</b> {String(v)}</span>)}</dd>
          <dt>Role from</dt><dd className="fine rule-text">{row.role_rule}</dd>
        </dl>
      </div>
    </aside>
  );
}
