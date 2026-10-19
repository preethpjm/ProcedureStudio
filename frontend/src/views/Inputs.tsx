import { useState } from "react";
import { api, Json, Project } from "../api";
import { plural, useStudio } from "../state";

/* Products and runs. A product is one assembly; a variant is a product derived from another product's
   signed-off baseline. Every run says what it is: the first issue, a new issue (revision), a modification
   to units in service (SB / MOD / ECN), or the first issue of a variant. Files not chosen are carried over. */

const KINDS: { kind: "step" | "mbom" | "drawing"; label: string; accept: string; hint: string }[] = [
  { kind: "step", label: "3D model", accept: ".stp,.step", hint: "STEP AP203/AP214 assembly" },
  { kind: "mbom", label: "Parts list", accept: ".pdf,.xlsx,.xls,.xlsm,.csv", hint: "MBOM, APL or EBOM: Excel, CSV or PDF" },
  { kind: "drawing", label: "Drawing", accept: ".pdf", hint: "Assembly drawing PDF with torques and notes (optional)" },
];
type ChangeKind = "initial" | "revision" | "modification" | "variant";
const CHANGE: Record<ChangeKind, { label: string; hint: string }> = {
  initial: { label: "First issue", hint: "The first run of this product. Sign it off to make it the baseline." },
  revision: { label: "New issue", hint: "A new drawing, 3D model or parts list for this product. Compared with the baseline; unchanged steps keep their approved text, item numbers continue." },
  modification: { label: "Modification (SB / MOD)", hint: "A change applied to units in service. Compared with the baseline, with a modification procedure for the change. Give the bulletin's reference; attach it if you like." },
  variant: { label: "Variant", hint: "" },
};
export const changeLabel = (c: Json) => !c ? "" : c.kind === "modification" ? (c.reference ?? "Modification") : CHANGE[c.kind as ChangeKind]?.label ?? c.kind;

const fmtBytes = (n: number) => n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`;

export function InputsView() {
  const { project, run, system, selectRun, toast } = useStudio();
  const rerun = async (stage: string) => {
    if (!run) return;
    try { await api.rerun(run.id, stage); await selectRun(run.id); } catch (e) { toast((e as Error).message, "bad"); }
  };
  const stages: { id: string; label: string }[] = system?.stages ?? [];

  if (!project) return <div className="view inputs"><NewProduct first /></div>;
  return (
    <div className="view inputs">
      <header className="product-head">
        <h1>{project.name}</h1>
        {project.parent && <p className="lede-s">Variant of <b>{project.parent.name}</b>, from its baseline {project.parent.label}. Runs are compared with it, and its approved procedure text is kept wherever nothing differs.</p>}
      </header>
      <NewRun project={project} />
      <NewProduct />

      {run && (
        <section className="stages">
          <h2>{run.label || run.id} <span className="sub">{run.change?.kind === "modification" ? (run.change.reference !== run.label ? `${run.change.reference}: ` : "") + (run.change.title ?? "Modification") : changeLabel(run.change)}</span></h2>
          {run.change?.reason && <p className="fine">Reason: {run.change.reason}</p>}
          {run.error && <p className="callout bad">{run.error}</p>}
          <ol className="stage-list">
            {stages.map((st) => {
              const r = run.stages?.[st.id];
              const active = run.progress?.stage === st.id && r?.status === "running";
              return (
                <li key={st.id} className={`stage ${r?.status ?? "pending"}`}>
                  <span className="stage-dot" aria-hidden />
                  <span className="stage-name">{st.label}</span>
                  <span className="stage-sum">{active ? run.progress.message : r?.status === "failed" ? r.error : summaryText(st.id, r?.summary)}</span>
                  <span className="stage-time">{r?.seconds != null ? `${r.seconds.toFixed(1)} s` : ""}</span>
                  {active && <span className="bar"><span style={{ width: `${Math.round((run.progress.fraction || 0) * 100)}%` }} /></span>}
                  {!run.signed_off && r && r.status !== "running" && !run.busy &&
                    <button className="link" onClick={() => rerun(st.id)} title="Run again from this stage">Run again</button>}
                </li>
              );
            })}
          </ol>
          <dl className="inputs-list">
            {Object.entries(run.inputs || {}).map(([k, v]) => (
              <div key={k}><dt>{KINDS.find((x) => x.kind === k)?.label ?? (k === "reference" ? "Bulletin" : k)}</dt>
                <dd><a href={`/api/runs/${run.id}/inputs/${k}`}>{v.file}</a> <span className="fine">{fmtBytes(v.bytes)}</span></dd></div>
            ))}
          </dl>
        </section>
      )}
    </div>
  );
}

function NewRun({ project }: { project: Project }) {
  const { selectProject, selectRun, toast } = useStudio();
  const isVariantStart = !!project.parent && project.runs.length === 0;
  // until a product has a baseline, runs are working runs of its first issue (a variant's are compared with its parent)
  const kinds: ChangeKind[] = project.baseline ? ["revision", "modification"] : project.parent ? ["variant"] : ["initial"];
  const [kind, setKind] = useState<ChangeKind>(kinds[0]);
  const k = kinds.includes(kind) ? kind : kinds[0];
  const [files, setFiles] = useState<Record<string, File | null>>({});
  const [refDoc, setRefDoc] = useState<File | null>(null);
  const [f, setF] = useState({ label: "", reference: "", title: "", reason: "" });
  const [sending, setSending] = useState(false);
  const prevInputs = (project.run_list?.[project.run_list.length - 1]?.inputs) ?? {};
  const inherited = isVariantStart ? `from ${project.parent!.name}` : "from the last run";
  const hasSource = project.runs.length > 0 || isVariantStart;
  const canStart = (hasSource || (files.step && files.mbom)) && (k !== "modification" || f.reference.trim());

  const start = async (e: React.FormEvent) => {
    e.preventDefault();
    const fd = new FormData();
    Object.entries(files).forEach(([n, v]) => v && fd.append(n, v));
    if (refDoc && k === "modification") fd.append("reference_doc", refDoc);
    fd.append("change_kind", k);
    fd.append("label", f.label);
    if (k === "modification") { fd.append("reference", f.reference); fd.append("change_title", f.title); }
    fd.append("reason", f.reason);
    setSending(true);
    try {
      const r = await api.createRun(project.id, fd);
      setFiles({}); setRefDoc(null); setF({ label: "", reference: "", title: "", reason: "" });
      await selectProject(project.id); await selectRun(r.id);
      toast(`${r.label} started`);
    } catch (err) { toast((err as Error).message, "bad"); }
    finally { setSending(false); }
  };

  return (
    <form className="new-run" onSubmit={start}>
      {kinds.length > 1 && <div className="seg" role="radiogroup" aria-label="What is this run?">
        {kinds.map((x) => <button type="button" key={x} role="radio" aria-checked={k === x} className={k === x ? "on" : ""} onClick={() => setKind(x)}>{CHANGE[x].label}</button>)}
      </div>}
      <p className="fine change-hint">{k === "variant" ? (isVariantStart ? `The first issue of this variant. Choose only the files that differ from ${project.parent!.name} (for example its parts list or 3D model); the others are taken from its baseline.` :
        `Another run of this variant's first issue, compared with ${project.parent!.name}. Files you do not choose are kept from the last run. Sign off a run to make it this variant's baseline.`) :
        k === "initial" && project.runs.length ? "Another run of the first issue. Files you do not choose are kept from the last run. Sign off a run to make it the baseline." : CHANGE[k].hint}</p>
      <div className="drop-grid">
        {KINDS.map((x) => {
          const prev = isVariantStart ? null : prevInputs[x.kind];
          const file = files[x.kind];
          return (
            <label key={x.kind} className={`drop ${file ? "has" : ""}`}>
              <span className="drop-label">{x.label}</span>
              <span className="drop-file">{file ? `${file.name} · ${fmtBytes(file.size)}` : prev ? `Keep ${prev.file}` : hasSource ? `Keep the file ${inherited}` : x.hint}</span>
              <input type="file" accept={x.accept} onChange={(e) => setFiles((o) => ({ ...o, [x.kind]: e.target.files?.[0] ?? null }))} />
              <span className="drop-action">{file ? "Replace" : hasSource ? "Choose a new file" : "Choose file"}</span>
            </label>
          );
        })}
      </div>
      {k === "modification" && <div className="fields mod-fields">
        <label><span>Reference (required)</span><input value={f.reference} onChange={(e) => setF({ ...f, reference: e.target.value })} placeholder="SB 32-0042, MOD 1234, ECN …" /></label>
        <label className="wide2"><span>What the modification does</span><input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} placeholder="Replace the plug screws with corrosion-resistant screws" /></label>
        <label className="file-field"><span>Bulletin document (optional)</span>
          <span className="btn file-btn">{refDoc ? refDoc.name : "Attach"}<input type="file" accept=".pdf,.docx,.doc,.xlsx,.txt,.xml,.zip" onChange={(e) => setRefDoc(e.target.files?.[0] ?? null)} /></span></label>
      </div>}
      <div className="drop-foot">
        <input aria-label="Run label" placeholder={k === "modification" ? "Label (default: the reference)" : "Label (default: Issue n)"} value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} />
        {k !== "initial" && <input aria-label="Reason for the change" placeholder="Reason for the change (optional)" value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} />}
        <button className="btn primary" disabled={!canStart || sending}>{sending ? "Uploading…" :
          k === "revision" ? "Start the new issue" : k === "modification" ? "Start the modification" : project.runs.length ? "Start a new run" : "Start the first run"}</button>
      </div>
    </form>
  );
}

function NewProduct({ first = false }: { first?: boolean }) {
  const { projects, refreshProjects, selectProject, toast } = useStudio();
  const [open, setOpen] = useState(first);
  const [name, setName] = useState("");
  const [parent, setParent] = useState("");
  const shipped = projects.filter((p) => p.baseline);
  if (!open) return <button className="link add-product" onClick={() => setOpen(true)}>Add a product or a variant</button>;
  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const p = await api.createProject(name.trim(), {}, parent || undefined);
      await refreshProjects(); await selectProject(p.id); setOpen(first); setName(""); setParent("");
      toast(parent ? "Variant created: start its first issue with the files that differ" : "Product created");
    } catch (err) { toast((err as Error).message, "bad"); }
  };
  return (
    <form className={first ? "lede" : "add-box new-product"} onSubmit={create}>
      {first ? <><h1>Start with a product</h1>
        <p>A product holds every issue of one assembly. Its first run, once signed off, is the baseline: later drawings, 3D models, parts lists and service bulletins are compared with it. A variant (a seat with a different cushion, a valve with another fitting) starts from a shipped product and records only what differs.</p></> :
        <h2>New product</h2>}
      <div className="fields">
        <label className="wide"><span>Name</span><input autoFocus={!first} value={name} onChange={(e) => setName(e.target.value)} placeholder={parent ? "For example: Pax seat 9G, leather cushion" : "For example: Brake selector valve D24907000"} /></label>
        <label className="wide"><span>Start from</span>
          <select value={parent} onChange={(e) => setParent(e.target.value)}>
            <option value="">Nothing: a new product</option>
            {shipped.map((p) => <option key={p.id} value={p.id}>Variant of {p.name} (baseline {p.run_list?.find((r) => r.id === p.baseline)?.label ?? p.last_run?.label ?? ""})</option>)}
          </select></label>
      </div>
      {parent && <p className="fine">The variant takes the parent's publication identity (with the next variant code), schema choices, parts-list mappings and wording rules. Its first run reuses the parent's files except the ones you replace.</p>}
      {!shipped.length && !first && <p className="fine">Variants start from a signed-off baseline; none exists yet.</p>}
      <div className="actions">
        <button className="btn primary" disabled={!name.trim()}>{parent ? "Create variant" : "Create product"}</button>
        {!first && <button type="button" className="link" onClick={() => setOpen(false)}>Cancel</button>}
      </div>
    </form>
  );
}

function summaryText(stage: string, s: Json): string {
  if (!s) return "";
  switch (stage) {
    case "convert": return `${s.occurrences} occurrences, ${s.products} distinct parts`;
    case "analyse": return `${s.contacts} contacts, ${s.steps} removal steps in ${s.layers} layers${s.conflicts ? `, ${s.conflicts} conflicts` : ""}`;
    case "reconcile": return `${s.parts_list_lines ? `${s.parts_list_lines} parts-list lines${s.parts_list_needs_review ? " (columns guessed: check the import)" : ""}; ` : ""}${s.matched} matched (${s.exact_matches} exact, ${s.fuzzy_matches} fuzzy)${s.unmatched_or_ambiguous_step ? `, ${plural(s.unmatched_or_ambiguous_step, "3D part")} unmatched` : ""}`;
    case "drawing": return `${s.torques} torques (${s.assigned} assigned), ${s.notes} notes`;
    case "ipl": return `${s.rows} rows, ${s.with_3d} in 3D, ${s.flagged} flagged${s.items_kept != null ? `; ${s.items_kept} item numbers kept, ${s.items_new} new, ${s.items_retired} retired` : ""}`;
    case "process": return `${s.disassembly_ops ?? 0} disassembly and ${s.assembly_ops ?? 0} assembly operations`;
    case "procedures": return `${s.disassembly?.steps ?? 0} disassembly and ${s.assembly?.steps ?? 0} assembly steps, ${(s.disassembly?.errors ?? 0) + (s.assembly?.errors ?? 0)} STE errors${(s.disassembly?.llm_steps ?? 0) + (s.assembly?.llm_steps ?? 0) ? `, ${(s.disassembly?.llm_steps ?? 0) + (s.assembly?.llm_steps ?? 0)} reworded by the LLM` : ""}`;
    case "revision": return s.changes != null ? `${plural(s.changes, "change")}, ${plural(s.affected_steps ?? 0, "step")} affected, ${s.kept_steps ?? 0} kept from the baseline` : "No baseline to compare with";
    case "check": return `${s.passed} of ${s.documents} documents pass${s.errors ? `, ${plural(s.errors, "error")}` : ""}${s.warnings ? `, ${plural(s.warnings, "warning")}` : ""}${s.not_validated ? `, ${s.not_validated} without an installed schema` : ""}`;
    default: return "";
  }
}
