import { useCallback, useEffect, useRef, useState } from "react";
import { api, Json } from "../api";
import { plural, useStudio } from "../state";
import { XmlCode } from "./xml/XmlCode";
import { XmlDoc } from "./xml/XmlDoc";

/* The generated documents as they will be published: checked against the installed schema, the BREX they
   name and the rule sets, shown through a view stylesheet, with every problem tied to its line. */

const STATUS_LABEL: Record<string, string> = {
  passed: "Passed", failed: "Failed", "not-run": "Not checked", "not-implemented": "Not available", unsupported: "No schema",
};
const STATUS_CLS: Record<string, string> = { passed: "ok", failed: "bad", "not-run": "", unsupported: "warn", "not-implemented": "" };

export function CheckView() {
  const { run, project, toast, touch, setView, selectProject, version, editable } = useStudio();
  const dirty = useRef(false);
  const [check, setCheck] = useState<Json | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [sel, setSel] = useState(0);
  const [busy, setBusy] = useState(false);
  const [conf, setConf] = useState<Json | null>(null);

  const load = () => run && api.check(run.id).then((c) => { setCheck(c); setErr(null); }).catch((e) => { setCheck(null); setErr(e.message); });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [run?.id, version]);
  useEffect(() => { api.conformance().then(setConf).catch(() => undefined); }, []);

  const again = async () => {
    if (!run) return;
    setBusy(true);
    try { const c = await api.checkAgain(run.id); setCheck(c); touch(); toast(c.summary.errors ? `Checked: ${plural(c.summary.errors, "error")}` : "Checked: no errors", c.summary.errors ? "bad" : "ok"); }
    catch (e) { toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  const chooseSchema = async (target: string, value: string) => {
    if (!project) return;
    const schemas = { ...(project.settings?.schemas ?? {}) };
    if (value) schemas[target] = value; else delete schemas[target];
    try { await api.saveSettings(project.id, { schemas }); await selectProject(project.id); await again(); }
    catch (e) { toast((e as Error).message, "bad"); }
  };

  if (!run) return null;
  const noSchemas = conf && conf.packages.length === 0;
  if (!check) {
    return (
      <div className="view checkdocs">
        <h1>Check documents</h1>
        <p className="lede-s">{err && !/not been checked/.test(err) ? err : "The documents are checked at the end of each run."}</p>
        <button className="btn primary" onClick={again} disabled={busy}>{busy ? "Checking…" : "Check the documents now"}</button>
      </div>
    );
  }
  const docs: Json[] = check.documents;
  const doc = docs[Math.min(sel, docs.length - 1)];
  const s = check.summary;

  return (
    <div className="view checkdocs">
      <header className="view-head">
        <h1>Check documents</h1>
        <p className="lede-s">
          {s.passed} of {plural(s.documents, "document")} pass. {s.errors ? `${plural(s.errors, "error")}, ` : ""}{plural(s.warnings, "warning")}.
          {" "}Checked {new Date(check.at).toLocaleString()} against what is installed in <button className="link" onClick={() => setView("schemas")}>Schemas and rules</button>.
        </p>
        <div className="toolbar">
          <button className="btn" onClick={again} disabled={busy}>{busy ? "Checking…" : "Check again"}</button>
          <span className="fine">Check again after adding schemas, BREX, rules or stylesheets.</span>
        </div>
        {noSchemas && <div className="callout warn">No schema package is installed, so the documents were only checked for well-formed XML.
          <button className="btn" onClick={() => setView("schemas")}>Add schemas</button></div>}
      </header>

      <div className="check-grid">
        <ol className="doc-list" aria-label="Documents">
          {docs.map((d, i) => {
            const r = d.report;
            const n = r.counts.error + r.counts.fatal;
            return (
              <li key={d.name}>
                <button className={i === sel ? "on" : ""} onClick={() => {
                  if (i !== sel && dirty.current && !window.confirm("This document has unsaved changes. Leave them?")) return;
                  dirty.current = false; setSel(i);
                }}>
                  <span className={`doc-dot ${r.passed ? "ok" : n ? "bad" : "warn"}`} aria-hidden />
                  <span className="doc-name">{docTitle(d)}</span>
                  <span className="doc-file">{d.name}{d.edited && <span className={`doc-edited ${d.edited.stale ? "stale" : ""}`}> · edited{d.edited.stale ? ", generated has changed" : ""}</span>}</span>
                  <span className="doc-count">{n ? plural(n, "error") : r.counts.warning ? plural(r.counts.warning, "warning") : "Clean"}</span>
                </button>
              </li>
            );
          })}
        </ol>
        {doc && <DocPanel key={doc.name} doc={doc} rid={run.id} conf={conf} checkedAt={check.at} editable={editable}
          choice={project?.settings?.schemas?.[doc.target] ?? ""} onChoose={(v) => chooseSchema(doc.target, v)}
          onChecked={(c) => { setCheck(c); touch(); }} onDirty={(b) => { dirty.current = b; }} />}
      </div>
    </div>
  );
}

const docTitle = (d: Json) => ({
  "S1000D-530": "Disassembly data module (530)", "S1000D-710": "Assembly data module (710)", "S1000D-941": "Illustrated parts data (941)",
  "ATA-CMM": "Component maintenance manual",
} as Record<string, string>)[d.key] ?? d.name;

type Tab = "problems" | "doc" | "xml" | "split";
const TABS: [Tab, string][] = [["doc", "Document"], ["problems", "Problems"], ["xml", "XML"], ["split", "Side by side"]];
const loadTags = () => { try { return localStorage.getItem("studio.tags") === "1"; } catch { return false; } };

function DocPanel({ doc, rid, conf, checkedAt, editable, choice, onChoose, onChecked, onDirty }: {
  doc: Json; rid: string; conf: Json; checkedAt: string; editable: boolean; choice: string; onChoose: (v: string) => void;
  onChecked: (c: Json) => void; onDirty: (b: boolean) => void;
}) {
  const { toast } = useStudio();
  const r = doc.report;
  const [tab, setTab] = useState<Tab>("doc");
  const [look, setLook] = useState("");                         // "" = the editable document; else a view stylesheet ("auto" or its id)
  const views: Json[] = (conf?.stylesheets ?? []).filter((x: Json) => x.kind === "view" && x.enabled !== false);
  const [tags, setTagsState] = useState(loadTags);
  const [goto, setGoto] = useState<{ line: number; n: number } | null>(null);
  const [saved, setSaved] = useState<string | null>(null);      // the document as checked
  const [draft, setDraft] = useState<string | null>(null);
  const [live, setLive] = useState<Json | null>(null);          // check of the unsaved text
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const id = r.identification ?? {};
  const options: Json[] = conf?.coverage?.[doc.target]?.options ?? [];
  const dirty = draft != null && saved != null && draft !== saved;
  const setTags = (b: boolean) => { setTagsState(b); try { localStorage.setItem("studio.tags", b ? "1" : "0"); } catch { /* private window */ } };

  // the checked text; again after every check (save, discard, check again)
  useEffect(() => {
    let off = false;
    api.documentText(rid, doc.name).then((t) => { if (!off) { setSaved(t); setDraft((d) => (d == null || d === saved ? t : d)); } })
      .catch((e) => !off && setSaved(`<!-- ${e.message} -->`));
    return () => { off = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rid, doc.name, checkedAt]);
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // live check of the unsaved text, a moment after typing stops
  useEffect(() => {
    if (!dirty || draft == null) { setLive(null); return; }
    setChecking(true);
    const t = window.setTimeout(() => {
      api.documentValidate(rid, doc.name, draft).then(setLive).catch(() => setLive(null)).finally(() => setChecking(false));
    }, 700);
    return () => window.clearTimeout(t);
  }, [draft, dirty, rid, doc.name]);

  const report = dirty && live ? live : r;
  const diags: Json[] = report.diagnostics;
  const nErr = report.counts.error + report.counts.fatal, nWarn = report.counts.warning;
  const toLine = useCallback((l: number | null | undefined) => {
    if (!l) return;
    setGoto((g) => ({ line: l, n: (g?.n ?? 0) + 1 }));
    setTab((t) => (t === "split" ? t : "xml"));
  }, []);

  const save = async () => {
    if (!draft || !dirty) return;
    setSaving(true);
    try {
      const c = await api.documentSave(rid, doc.name, draft);
      const mine = c.documents.find((x: Json) => x.name === doc.name);
      setSaved(draft);
      onChecked(c);
      const n = mine ? mine.report.counts.error + mine.report.counts.fatal : 0;
      toast(n ? `Saved. ${plural(n, "error")} still to fix` : "Saved and checked: no errors", n ? "bad" : "ok");
    } catch (e) { toast((e as Error).message, "bad"); }
    finally { setSaving(false); }
  };
  const revert = () => { if (saved != null) setDraft(saved); };
  const discard = async () => {
    if (!window.confirm("Go back to the generated document? Your edit of this document is removed.")) return;
    try { const c = await api.documentDiscard(rid, doc.name); setDraft(null); onChecked(c); toast("Back to the generated document", "ok"); }
    catch (e) { toast((e as Error).message, "bad"); }
  };
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (editable) save(); } };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });

  const editing = tab === "doc" || tab === "xml" || tab === "split";
  const ro = !editable;
  const status = checking ? { cls: "pending", text: "Checking…" }
    : nErr ? { cls: "bad", text: plural(nErr, "error") + (nWarn ? `, ${plural(nWarn, "warning")}` : "") }
    : nWarn ? { cls: "warn", text: plural(nWarn, "warning") } : { cls: "ok", text: "Valid" };

  return (
    <section className="doc-panel" aria-label={doc.name}>
      <div className="doc-head">
        <h2>{docTitle(doc)}</h2>
        <p className="fine">{doc.name}
          {doc.edited && <span className={`edit-badge ${doc.edited.stale ? "stale" : ""}`}>
            {doc.edited.stale ? "Edited by hand; the generated document has changed since" : `Edited by hand ${new Date(doc.edited.at).toLocaleString()}`}</span>}
        </p>
        <dl className="facts">
          <dt>Schema</dt>
          <dd>{id.package_id ? <>{id.package_id} · {id.doc_type} <span className="fine">({id.how})</span></> : <span className="warn-t">No installed schema fits this document</span>}</dd>
          {options.length > 0 && <>
            <dt><label htmlFor="schema-choice">Validate against</label></dt>
            <dd><select id="schema-choice" value={choice} onChange={(e) => onChoose(e.target.value)} disabled={dirty}>
              <option value="">The schema the document declares</option>
              {options.map((o) => <option key={o.package_id + o.doc_type} value={`${o.package_id}:${o.doc_type}`}>{o.label}{o.provenance === "synthetic" ? " (evaluation)" : ""}</option>)}
            </select></dd>
          </>}
          {doc.transforms?.length > 0 && <><dt>Transforms</dt><dd>{doc.transforms.map((t: Json) => t.name).join(", ")}</dd></>}
        </dl>
        <ol className="stage-strip">
          {report.stages.map((st: Json) => (
            <li key={st.stage} className={STATUS_CLS[st.status] ?? ""} title={st.note}>
              <span className="stage-n">{st.stage}</span>
              <span className="stage-t">{st.name}</span>
              <span className="stage-s">{STATUS_LABEL[st.status] ?? st.status}</span>
              {st.note && <span className="stage-note">{st.note}</span>}
            </li>
          ))}
        </ol>
      </div>
      <div className="tabs" role="tablist">
        {TABS.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>
            {label}{k === "problems" && <span className={`tab-count ${nErr ? "bad" : nWarn ? "warn" : "ok"}`}>{diags.length}</span>}
          </button>
        ))}
        <a className="link tab-link" href={api.documentUrl(rid, doc.name)} download={doc.name}>Download</a>
      </div>
      {(editing || dirty) && (
        <div className={`edit-bar ${dirty ? "dirty" : ""}`}>
          <span className={`live ${status.cls}`} role="status">{status.text}</span>
          <span className="fine">{ro ? "Signed-off baseline: read only. Start a new run to change it."
            : dirty ? "Unsaved changes, checked as you type." : doc.edited ? "Saved edit: this is what is published." : "Edit the text in Document, the markup in XML."}</span>
          {(tab === "doc" || tab === "split") && !look && (
            <label className="switch"><input type="checkbox" checked={tags} onChange={(e) => setTags(e.target.checked)} /> Tags</label>
          )}
          {tab === "doc" && (
            <label className="look fine">Show
              <select value={look} onChange={(e) => setLook(e.target.value)}>
                <option value="">Document (edit here)</option>
                <option value="auto">Print preview: built-in stylesheet</option>
                {views.map((v) => <option key={v.id} value={v.id}>Print preview: {v.name}</option>)}
              </select>
            </label>
          )}
          <span className="edit-actions">
            {doc.edited && !dirty && !ro && <button className="btn quiet" onClick={discard}>Back to generated</button>}
            {dirty && <button className="btn quiet" onClick={revert}>Revert</button>}
            {!ro && <button className="btn primary" onClick={save} disabled={!dirty || saving} title="Ctrl+S">{saving ? "Saving…" : "Save and check"}</button>}
          </span>
        </div>
      )}
      {tab === "problems" && <Diagnostics items={diags} onLine={toLine} />}
      {editing && draft == null && <p className="fine">Loading…</p>}
      {editing && draft != null && (
        <div className={`editor ${tab}`}>
          {tab === "doc" && look &&
            <div className="editor-pane"><Preview rid={rid} name={doc.name} sheet={look === "auto" ? "" : look} draft={dirty ? draft : null} /></div>}
          {((tab === "doc" && !look) || tab === "split") &&
            <div className="editor-pane"><XmlDoc text={draft} onChange={setDraft} readOnly={ro} tags={tags} diagnostics={diags} onLine={toLine} /></div>}
          {(tab === "xml" || tab === "split") &&
            <div className="editor-pane code"><XmlCode text={draft} onChange={setDraft} readOnly={ro} diagnostics={diags} gotoLine={goto} /></div>}
        </div>
      )}
    </section>
  );
}

export function Diagnostics({ items, onLine }: { items: Json[]; onLine?: (l: number) => void }) {
  if (!items.length) return <p className="clean">No problems found.</p>;
  const order = { fatal: 0, error: 1, warning: 2, info: 3 } as Record<string, number>;
  const sorted = [...items].sort((a, b) => (order[a.severity] - order[b.severity]) || (a.stage - b.stage) || ((a.line ?? 0) - (b.line ?? 0)));
  return (
    <ul className="diags">
      {sorted.map((d, i) => (
        <li key={i} className={`diag-item ${d.severity}`}>
          <div className="diag-top">
            <span className={`sev ${d.severity}`}>{d.severity}</span>
            <code className="rule">{d.rule_id}</code>
            {d.line && onLine ? <button className="link" onClick={() => onLine(d.line)}>line {d.line}</button> : d.line ? <span className="fine">line {d.line}</span> : null}
            {d.element_path && <span className="path" title={d.element_path}>{d.element_path}</span>}
          </div>
          <p className="diag-msg">{d.message}</p>
          {d.suggestion && <p className="diag-sugg">{d.suggestion}</p>}
          {d.reference && <p className="fine">{d.reference}</p>}
        </li>
      ))}
    </ul>
  );
}

/** The document through a view stylesheet (read only): what a print or a portal shows. */
function Preview({ rid, name, sheet, draft }: { rid: string; name: string; sheet: string; draft: string | null }) {
  const [html, setHtml] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setErr(null);
    const p = draft != null ? api.documentViewText(rid, name, draft, sheet || undefined) : api.documentView(rid, name, sheet || undefined);
    p.then((v) => setHtml(v.html)).catch((e) => setErr(e.message));
  }, [rid, name, sheet, draft]);
  return err ? <p className="callout bad">{err}</p> : html == null ? <p className="fine">Rendering…</p> :
    <iframe className="doc-frame preview" title={`Print preview of ${name}`} sandbox="" srcDoc={html} />;
}
