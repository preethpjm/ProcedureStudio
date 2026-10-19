import { useEffect, useMemo, useRef, useState } from "react";
import { api, Json } from "../api";
import { plural, useArtifact, useStudio } from "../state";

/* STE procedures generated from the process graph. Each step names the IPL items it acts on, so selecting a
   step shows those parts. Writers can rewrite a step; the STE checker and the fact check run on every save. */

type Kind = "disassembly" | "assembly" | "modification";

export function ProceduresView() {
  const { run, procKind, setProcKind, system, editable, toast, selectRun } = useStudio();
  const llmOn = !!system?.llm?.available;
  const reword = async () => {
    if (!run) return;
    try { await api.rerun(run.id, "procedures"); await selectRun(run.id); toast(`Rewording with ${system.llm.provider}${system.llm.model ? ` (${system.llm.model})` : ""}; every step is checked again.`); }
    catch (e) { toast((e as Error).message, "bad"); }
  };
  const hasMod = run?.stages?.revision?.status === "done" && !!run?.base_run && (run?.stages?.revision?.summary?.changes ?? 0) > 0;
  const kind = procKind as Kind, setKind = setProcKind;
  useEffect(() => { if (kind === "modification" && !hasMod) setKind("disassembly"); }, [hasMod, kind]);
  const { data: proc } = useArtifact<Json>(`procedure_${kind}`);
  const { data: ipl } = useArtifact<Json>("ipl");
  const llm = run?.stages?.procedures?.summary?.[kind]?.llm_steps;
  // count from the steps themselves so writer edits show up at once
  const all: Json[] = proc?.subtasks?.flatMap((st: Json) => st.steps) ?? [];
  const iss: Json[] = all.flatMap((x) => x.issues ?? []);
  const nErr = iss.filter((i) => i.severity === "error").length, nWarn = iss.length - nErr;

  return (
    <div className="view procedures">
      <header className="view-head">
        <h1>{proc?.title ?? "Procedures"}</h1>
        <div className="tabs" role="tablist">
          {(["disassembly", "assembly", ...(hasMod ? ["modification"] : [])] as Kind[]).map((k) =>
            <button key={k} role="tab" aria-selected={kind === k} className={kind === k ? "on" : ""} onClick={() => setKind(k)}>
              {k === "disassembly" ? "Disassembly" : k === "assembly" ? "Assembly" : run?.change?.kind === "variant" ? "Conversion" : "Modification"}</button>)}
          {llmOn && editable && kind !== "modification" && <button className="btn quiet tab-btn" onClick={reword} title="Generate the steps again and let the language model improve the wording">Reword with the language model</button>}
        </div>
        {proc && <p className="lede-s">
          {plural(all.length, "step")} in {plural(proc.subtasks?.length ?? 0, "subtask")}.{" "}
          {iss.length === 0 ? "No STE issues." : `${plural(nErr, "STE error")}, ${plural(nWarn, "STE warning")}.`}
          {" "}{llm ? `${llm} steps reworded by the LLM and checked.` : "Written from templates."}
          {kind === "modification" && " This procedure covers only what the change touches."}
        </p>}
      </header>
      {!proc ? <p className="fine">Loading…</p> : <ProcBody proc={proc} ipl={ipl} kind={kind} />}
    </div>
  );
}

function ProcBody({ proc, ipl, kind }: { proc: Json; ipl: Json; kind: Kind }) {
  const { run, setScene, pickHandler, editable, toast, touch } = useStudio();
  const [sel, setSel] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const rowOcc = useMemo(() => new Map<string, string[]>((ipl?.rows ?? []).map((r: Json) => [r.id, r.occ_ids ?? []])), [ipl]);
  const allSteps: Json[] = useMemo(() => proc.subtasks.flatMap((st: Json) => st.steps), [proc]);
  const occOf = (s: Json): string[] => s.occ_ids?.length ? s.occ_ids : (s.rows ?? []).flatMap((r: string) => rowOcc.get(r) ?? []);

  useEffect(() => {
    const s = allSteps.find((x) => x.id === sel);
    const occ = s ? occOf(s) : [];
    setScene({ selected: new Set(occ), dim: occ.length ? new Set(occ) : null, tint: undefined, explode: undefined, hidden: undefined });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel, allSteps, rowOcc, setScene]);
  useEffect(() => () => setScene({ dim: null }), [setScene]);

  useEffect(() => {
    pickHandler.current = (id) => {
      if (!id) { setSel(null); return; }
      const s = allSteps.find((x) => occOf(x).includes(id));
      if (s) { setSel(s.id); document.getElementById(`step-${s.id}`)?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
    };
    return () => { pickHandler.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allSteps, rowOcc, pickHandler]);

  const save = async (s: Json, text: string) => {
    if (!run) return;
    try {
      const r = await api.editStep(run.id, kind, s.id, text);
      setEditing(null);
      toast(r.issues?.length ? `Saved with ${plural(r.issues.length, "issue")} to check` : "Saved. No STE issues.", r.issues?.some((i: Json) => i.severity === "error") ? "bad" : "ok");
      touch();
    } catch (e) { toast((e as Error).message, "bad"); }
  };

  const pre = proc.preliminary ?? {};
  const safety: Record<string, Json> = proc.safety ?? {};
  let n = 0;
  return (
    <div className="proc">
      {kind === "modification" && proc.changes?.length > 0 && <div className="callout info"><b>Changes covered</b><ul>{proc.changes.map((c: string, i: number) => <li key={i}>{c}</li>)}</ul></div>}
      <section className="prelim">
        <PreList title="Support equipment" items={(pre.support_equipment ?? []).map((x: Json) => x.name)} empty="None" />
        <PreList title="Supplies" items={(pre.supplies ?? []).map((x: Json) => x.name)} empty="None" />
        <PreList title="Spares" items={(pre.spares ?? []).map((x: Json) => `${x.qty} × ${x.name} (${x.item}) ${x.part_number}`)} empty="None" />
        {(proc.common_info ?? []).length > 0 && <PreList title="General" items={proc.common_info} empty="" />}
      </section>
      {(pre.warnings ?? []).map((w: string) => safety[w] && <Safety key={w} s={safety[w]} />)}

      {proc.subtasks.map((st: Json) => (
        <section key={st.id} className="subtask">
          <h2>{st.title}</h2>
          {[...(st.warnings ?? []), ...(st.cautions ?? [])].map((w: string) => safety[w] && <Safety key={w} s={safety[w]} />)}
          <ol className="steps">
            {st.steps.map((s: Json) => {
              n += 1;
              const issues: Json[] = s.issues ?? [];
              const isSel = sel === s.id;
              return (
                <li key={s.id} id={`step-${s.id}`} className={["step", isSel && "sel", issues.some((i) => i.severity === "error") && "err", s.revision && s.revision !== "unchanged" && "rev"].filter(Boolean).join(" ")}
                  onClick={() => setSel(isSel ? null : s.id)}>
                  <span className="step-no">{n}</span>
                  <div className="step-body">
                    {editing === s.id ? <StepEditor text={s.text} onSave={(t) => save(s, t)} onCancel={() => setEditing(null)} /> :
                      <p className="step-text">{s.text}</p>}
                    <div className="step-meta">
                      {s.revision && s.revision !== "unchanged" && <span className="tag info">{s.revision === "changed" ? "Changed from baseline" : s.revision === "new" ? "New in this revision" : s.revision}</span>}
                      {s.origin === "writer" && <span className="tag">Edited by writer</span>}
                      {s.origin === "llm" && <span className="tag">Reworded by LLM</span>}
                      {!occOf(s).length && !(s.flags ?? []).length && <span className="fine">Not shown in 3D</span>}
                      {editable && kind !== "modification" && editing !== s.id &&
                        <button className="link" onClick={(e) => { e.stopPropagation(); setEditing(s.id); }}>Edit</button>}
                    </div>
                    {issues.map((i, k) => <p key={k} className={`issue ${i.severity}`}><b>{i.rule}</b> {i.message}</p>)}
                    {(s.flags ?? []).map((f: string, k: number) => <p key={k} className="flag">{f}</p>)}
                  </div>
                </li>
              );
            })}
          </ol>
        </section>
      ))}
    </div>
  );
}

function StepEditor({ text, onSave, onCancel }: { text: string; onSave: (t: string) => void; onCancel: () => void }) {
  const [t, setT] = useState(text);
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { ref.current?.focus(); }, []);
  const words = t.trim().split(/\s+/).filter(Boolean).length;
  return (
    <div className="step-editor" onClick={(e) => e.stopPropagation()}>
      <textarea ref={ref} value={t} rows={2} onChange={(e) => setT(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onSave(t); if (e.key === "Escape") onCancel(); }} />
      <div className="actions">
        <span className={`fine ${words > 20 ? "warn-t" : ""}`}>{words} words{words > 20 ? ": STE allows 20 in an instruction" : ""}</span>
        <button className="btn primary" disabled={!t.trim() || t === text} onClick={() => onSave(t)}>Save and check</button>
        <button className="link" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

function PreList({ title, items, empty }: { title: string; items: string[]; empty: string }) {
  return <div className="pre"><h3>{title}</h3>{items.length ? <ul>{items.map((x, i) => <li key={i}>{x}</li>)}</ul> : <p className="fine">{empty}</p>}</div>;
}

function Safety({ s }: { s: Json }) {
  return (
    <div className={`safety ${s.kind}`} title={(s.evidence ?? []).join("\n")}>
      <b>{s.kind === "warning" ? "WARNING" : "CAUTION"}</b>
      <p>{s.text}</p>
    </div>
  );
}
