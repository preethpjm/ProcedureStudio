import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { StudioProvider, useStudio, ViewId } from "./state";
import { Viewer } from "./Viewer";
import { changeLabel, InputsView } from "./views/Inputs";
import { MatchView } from "./views/Match";
import { IplView } from "./views/Ipl";
import { SequenceView } from "./views/Sequence";
import { ProceduresView } from "./views/Procedures";
import { PublishView } from "./views/Publish";
import { ChangesView } from "./views/Changes";
import { CheckView } from "./views/Check";
import { SchemasView } from "./views/Schemas";
import { SettingsView } from "./views/Settings";

/* The rail reads like a shop routing sheet: each operation has a number, a name, and an inspection stamp
   that shows whether the writer has something to look at there. */
interface Op { id: ViewId; no: string; name: string; stages: string[] }
const OPS: Op[] = [
  { id: "inputs", no: "010", name: "Inputs and run", stages: ["convert"] },
  { id: "match", no: "020", name: "Match 3D to parts list", stages: ["reconcile"] },
  { id: "ipl", no: "030", name: "Illustrated parts list", stages: ["ipl", "drawing"] },
  { id: "sequence", no: "040", name: "Disassembly sequence", stages: ["analyse"] },
  { id: "procedures", no: "050", name: "Procedures", stages: ["process", "procedures"] },
  { id: "check", no: "060", name: "Check documents", stages: ["check"] },
  { id: "publish", no: "070", name: "Publish", stages: [] },
];
const CHANGES: Op = { id: "changes", no: "080", name: "Changes from baseline", stages: ["revision"] };

export default function App() {
  const [toasts, setToasts] = useState<{ id: number; msg: string; kind: string }[]>([]);
  const toast = useCallback((msg: string, kind: "ok" | "bad" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, msg, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "bad" ? 7000 : 3200);
  }, []);
  return (
    <StudioProvider toast={toast}>
      <Shell />
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{t.msg}</div>)}
      </div>
    </StudioProvider>
  );
}

function stamp(run: ReturnType<typeof useStudio>["run"], op: Op): { cls: string; text: string; title: string } {
  if (!run) return { cls: "", text: "", title: "No run yet" };
  const st = op.stages.map((s) => run.stages?.[s]).filter(Boolean);
  if (st.some((s) => s!.status === "failed")) return { cls: "bad", text: "!", title: "This step failed" };
  if (st.some((s) => s!.status === "running")) return { cls: "run", text: "", title: "Running" };
  if (op.stages.length && st.length < op.stages.length) return { cls: "wait", text: "", title: "Not run yet" };
  const sum = (k: string) => run.stages?.[k]?.summary || {};
  if (op.id === "ipl") { const f = sum("ipl").flagged; return f ? { cls: "warn", text: String(f), title: `${f} rows to check` } : { cls: "ok", text: "✓", title: "Nothing flagged" }; }
  if (op.id === "match") { const u = (sum("reconcile").unmatched_or_ambiguous_step ?? 0); return u ? { cls: "warn", text: String(u), title: `${u} 3D parts not matched` } : { cls: "ok", text: "✓", title: "All 3D parts matched" }; }
  if (op.id === "sequence") { const c = sum("analyse").conflicts; return c ? { cls: "warn", text: String(c), title: `${c} conflicts` } : { cls: "ok", text: "✓", title: "No conflicts" }; }
  if (op.id === "procedures") {
    const p = sum("procedures"); const e = (p.disassembly?.errors ?? 0) + (p.assembly?.errors ?? 0);
    return e ? { cls: "warn", text: String(e), title: `${e} STE errors` } : { cls: "ok", text: "✓", title: "No STE errors" };
  }
  if (op.id === "check") {
    const c = sum("check");
    return c.errors ? { cls: "warn", text: String(c.errors), title: `${c.errors} validation errors` } :
      c.not_validated ? { cls: "info", text: "?", title: "No schema installed for some documents" } : { cls: "ok", text: "✓", title: "All documents pass" };
  }
  if (op.id === "changes") { const c = sum("revision").changes; return { cls: c ? "info" : "ok", text: c ? String(c) : "✓", title: c ? `${c} changes` : "No changes" }; }
  if (op.id === "publish") return run.signed_off ? { cls: "ok", text: "✓", title: "Signed off" } : { cls: "", text: "", title: "" };
  return run.status === "done" ? { cls: "ok", text: "✓", title: "Done" } : { cls: "", text: "", title: "" };
}

function Shell() {
  const s = useStudio();
  const { run, project, view, setView, scene, pickHandler, setNodes } = s;
  const ops = run?.base_run ? [...OPS, CHANGES] : OPS;
  useEffect(() => { if (view === "changes" && run && !run.base_run) setView("ipl"); }, [view, run, setView]);
  const busy = !!run && (run.busy || run.status === "running" || run.status === "queued");
  const showViewer = !["inputs", "publish", "check", "schemas", "settings"].includes(view);
  const modelUrl = run?.stages?.convert?.status === "done" ? `${api.modelUrl(run.id)}?v=${run.inputs?.step?.sha256?.slice(0, 8)}` : null;

  return (
    <div className="app">
      <header className="top">
        <div className="brand"><span className="brand-mark" aria-hidden />Procedure Studio</div>
        <ProjectPicker />
        <div className="top-spacer" />
        {busy && <div className="top-progress" title={run?.progress?.message}>
          <span className="spinner" aria-hidden />{run?.progress?.message || "Working"}
        </div>}
        <SystemLights />
      </header>

      <nav className="rail" aria-label="Workflow">
        <ol>
          {ops.map((op) => {
            const st = stamp(run, op);
            return (
              <li key={op.id}>
                <button className={`op ${view === op.id ? "on" : ""}`} onClick={() => setView(op.id)} aria-current={view === op.id ? "step" : undefined}>
                  <span className="op-no">{op.no}</span>
                  <span className="op-name">{op.name}</span>
                  <span className={`stamp ${st.cls}`} title={st.title}>{st.text}</span>
                </button>
              </li>
            );
          })}
        </ol>
        <button className={`op lib ${view === "schemas" ? "on" : ""}`} onClick={() => setView("schemas")} aria-current={view === "schemas" ? "page" : undefined}>
          <span className="op-no" aria-hidden>§</span><span className="op-name">Schemas and rules</span><span />
        </button>
        <button className={`op lib2 ${view === "settings" ? "on" : ""}`} onClick={() => setView("settings")} aria-current={view === "settings" ? "page" : undefined}>
          <span className="op-no" aria-hidden>⚙</span><span className="op-name">Settings</span><span />
        </button>
        {run && <RunCard />}
      </nav>

      <main className={`work ${showViewer ? "with-viewer" : ""}`}>
        <section className="panel" aria-label={ops.find((o) => o.id === view)?.name}>
          {view === "schemas" ? <SchemasView /> : view === "settings" ? <SettingsView /> :
            !project ? <InputsView /> :
            view === "inputs" ? <InputsView /> :
            !run ? <Empty text="Start a run from Inputs and run to see this step." /> :
            view === "match" ? <MatchView /> :
            view === "ipl" ? <IplView /> :
            view === "sequence" ? <SequenceView /> :
            view === "procedures" ? <ProceduresView /> :
            view === "check" ? <CheckView /> :
            view === "publish" ? <PublishView /> :
            view === "changes" ? <ChangesView /> : null}
        </section>
        <section className="stage3d" aria-label="3D model" hidden={!showViewer}>
          <Viewer url={modelUrl} {...scene} onPick={(id, add) => pickHandler.current?.(id, add)} onNodes={setNodes} />
        </section>
      </main>
    </div>
  );
}

export function Empty({ text, children }: { text: string; children?: React.ReactNode }) {
  return <div className="empty"><p>{text}</p>{children}</div>;
}

function ProjectPicker() {
  const { projects, project, run, selectProject, selectRun } = useStudio();
  // products first, each followed by its variants
  const roots = projects.filter((p) => !p.parent || !projects.some((q) => q.id === p.parent!.project));
  const kids = (id: string) => projects.filter((p) => p.parent?.project === id);
  const ordered: { p: typeof projects[number]; depth: number }[] = [];
  const walk = (p: typeof projects[number], depth: number) => { ordered.push({ p, depth }); kids(p.id).forEach((k) => walk(k, depth + 1)); };
  roots.sort((a, b) => a.name.localeCompare(b.name)).forEach((p) => walk(p, 0));
  return (
    <div className="picker">
      <label>
        <span className="sr">Product</span>
        <select value={project?.id ?? ""} onChange={(e) => selectProject(e.target.value || null)}>
          {!project && <option value="">Choose a product</option>}
          {ordered.map(({ p, depth }) => <option key={p.id} value={p.id}>{"\u2003".repeat(depth)}{depth ? "↳ " : ""}{p.name}</option>)}
        </select>
      </label>
      {project && project.runs.length > 0 && (
        <label>
          <span className="sr">Run</span>
          <select value={run?.id ?? ""} onChange={(e) => selectRun(e.target.value)}>
            {(project.run_list ?? []).map((r) =>
              <option key={r.id} value={r.id}>{(r.label || r.id) + (project.baseline === r.id ? " · baseline" : "")}</option>)}
          </select>
        </label>
      )}
    </div>
  );
}

function SystemLights() {
  const { system } = useStudio();
  if (!system) return null;
  const conv = system.converter_status;
  const llm = system.llm;
  const conns: { name: string; available: boolean; error?: string; url?: string; path?: string }[] = system.connectors_status ?? [];
  const cf = system.conformance ?? {};
  return (
    <ul className="lights" aria-label="Connections">
      <li className={conv?.available ? "ok" : "bad"} title={conv?.detail || ""}>STEP converter</li>
      <li className={llm?.available ? "ok" : llm?.provider && llm.provider !== "none" ? "bad" : "off"} title={llm?.available ? `${llm.provider} ${llm.model ?? ""}` : "Steps are written from templates. Set an LLM provider to refine wording."}>
        {llm?.available ? `LLM: ${llm.provider}` : "LLM off"}</li>
      <li className={cf.packages ? "ok" : "bad"} title={`${cf.packages ?? 0} schema packages, ${cf.brex ?? 0} BREX, ${cf.rules ?? 0} rule sets`}>
        {cf.packages ? `Schemas: ${cf.packages}` : "No schemas"}</li>
      {conns.map((c) => <li key={c.name} className={c.available ? "ok" : "bad"} title={c.error || c.url || c.path || ""}>
        {c.name === "folder" ? "Folder" : c.name}</li>)}
    </ul>
  );
}

function RunCard() {
  const { run, project, selectProject, toast, refreshRun } = useStudio();
  if (!run || !project) return null;
  const base = run.base_run ? (project.run_list?.find((r) => r.id === run.base_run)?.label ??
    (project.parent?.run === run.base_run ? `${project.parent.name}, ${project.parent.label}` : run.base_run)) : null;
  const signoff = async () => {
    if (!confirm(`Sign off ${run.label || run.id} as the baseline? It becomes read-only, and later runs are compared with it.`)) return;
    try { await api.signoff(run.id); toast("Signed off as the baseline"); await selectProject(project.id); await refreshRun(); }
    catch (e) { toast((e as Error).message, "bad"); }
  };
  return (
    <div className="runcard">
      <div className="runcard-title">{run.label || run.id}</div>
      <dl>
        {run.change && run.change.kind !== "initial" && <><dt>Change</dt><dd>{changeLabel(run.change)}</dd></>}
        <dt>Started</dt><dd>{new Date(run.created).toLocaleString()}</dd>
        {base && <><dt>Compared with</dt><dd>{base}</dd></>}
        <dt>Status</dt><dd>{run.signed_off ? `Baseline, signed off ${new Date(run.signed_off.at).toLocaleDateString()}` : run.status === "done" ? "Ready for review" : run.status}</dd>
      </dl>
      {!run.signed_off && run.status === "done" && <button className="btn quiet" onClick={signoff}>Sign off as baseline</button>}
    </div>
  );
}
