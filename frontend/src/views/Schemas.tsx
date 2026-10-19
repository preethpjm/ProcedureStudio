import { useEffect, useRef, useState } from "react";
import { api, Json } from "../api";
import { plural, useStudio } from "../state";
import { Diagnostics } from "./Check";

/* Everything documents are checked against, for every product: schema packages, BREX, rule sets,
   stylesheets. Changes apply the next time documents are checked. */

type Tab = "schemas" | "brex" | "rules" | "stylesheets" | "try";
const STANDARDS = ["S1000D", "ATA2200", "ATA2300", "S2000M", "S3000L", "S4000P", "S5000F", "S6000T", "OEM"];

export function SchemasView() {
  const { toast, refreshSystem } = useStudio();
  const [tab, setTab] = useState<Tab>("schemas");
  const [conf, setConf] = useState<Json | null>(null);
  const reload = () => api.conformance().then(setConf).catch((e) => toast(e.message, "bad"));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { reload(); }, []);
  const act = async (p: Promise<unknown>, ok: string) => {
    try { await p; toast(ok); await reload(); refreshSystem(); return true; } catch (e) { toast((e as Error).message, "bad"); return false; }
  };
  if (!conf) return <div className="view"><p className="fine">Loading…</p></div>;
  const counts: Record<Tab, number | null> = { schemas: conf.packages.length, brex: conf.brex.length, rules: conf.rules.length,
    stylesheets: conf.stylesheets.length, try: null };

  return (
    <div className="view schemas">
      <header className="view-head">
        <h1>Schemas and rules</h1>
        <p className="lede-s">What every product's documents are checked against: the schema of the standard, the BREX the documents name, your own rule sets, and the stylesheets that show or transform them. Changes apply the next time documents are checked.</p>
      </header>
      <div className="tabs" role="tablist">
        {([["schemas", "Schemas"], ["brex", "BREX"], ["rules", "Rule sets"], ["stylesheets", "Stylesheets"], ["try", "Try a document"]] as [Tab, string][]).map(([k, l]) =>
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>
            {l}{counts[k] != null ? ` (${counts[k]})` : ""}</button>)}
      </div>
      {tab === "schemas" && <SchemaPackages conf={conf} act={act} reload={reload} />}
      {tab === "brex" && <BrexPanel conf={conf} act={act} />}
      {tab === "rules" && <RulesPanel conf={conf} act={act} />}
      {tab === "stylesheets" && <StylesPanel conf={conf} act={act} />}
      {tab === "try" && <TryPanel />}
    </div>
  );
}

type Act = (p: Promise<unknown>, ok: string) => Promise<boolean>;

// ---------------------------------------------------------------- schema packages
function SchemaPackages({ conf, act, reload }: { conf: Json; act: Act; reload: () => void }) {
  const { toast, refreshSystem } = useStudio();
  const [proposal, setProposal] = useState<Json | null>(null);
  const [sending, setSending] = useState(false);
  const folder = useRef<HTMLInputElement>(null);
  useEffect(() => { folder.current?.setAttribute("webkitdirectory", ""); }, []);

  const inspect = async (list: FileList | null) => {
    if (!list?.length) return;
    const files = Array.from(list);
    setSending(true);
    try {
      const p = await api.inspectSchemas(files, files.map((f) => (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name));
      setProposal(p);
    } catch (e) { toast((e as Error).message, "bad"); }
    finally { setSending(false); }
  };

  return (
    <section className="panel-sec">
      <div className="sec-head">
        <p className="fine">Add an issue of a standard as you received it: the zip or folder of the S1000D schemas (the default BREX inside is added too), an ATA or OEM DTD set, any XSD set, or a ready-made schema package. Studio works out the document types and asks only what it cannot read from the files.</p>
        <div className="toolbar">
          <label className="btn primary file-btn">{sending ? "Reading…" : "Add schemas from a zip or files"}
            <input type="file" multiple accept=".zip,.xsd,.dtd,.ent,.mod,.cat,.xml,.json" onChange={(e) => { inspect(e.target.files); e.target.value = ""; }} /></label>
          <label className="btn file-btn">Add a folder
            <input ref={folder} type="file" multiple onChange={(e) => { inspect(e.target.files); e.target.value = ""; }} /></label>
          {conf.evaluation_packages && <button className="btn quiet" onClick={() => act(api.installEvaluation(), "Evaluation schemas installed")}
            title="Synthetic subsets written for testing. Use the official schemas for real checks.">Install evaluation subsets</button>}
        </div>
      </div>
      {proposal && <Proposal p={proposal} onDone={() => { setProposal(null); reload(); refreshSystem(); }} onCancel={() => setProposal(null)} />}

      <h2>Coverage of Studio's outputs</h2>
      <table className="grid compact static">
        <thead><tr><th>Output</th><th>Installed schemas that validate it</th></tr></thead>
        <tbody>{Object.entries(conf.coverage).map(([k, v]: [string, Json]) => (
          <tr key={k}><td>{v.label}</td><td>{v.options.length ? v.options.map((o: Json) => <div key={o.package_id + o.doc_type}>{o.label}{o.provenance === "synthetic" && <span className="tag warn">evaluation</span>}</div>) : <span className="warn-t">None installed: documents are checked for well-formed XML only</span>}</td></tr>
        ))}</tbody>
      </table>

      <h2>Installed schema packages</h2>
      {!conf.packages.length ? <p className="fine">None yet.</p> : (
        <ul className="lib-list">
          {conf.packages.map((p: Json) => (
            <li key={p.id} className={p.enabled ? "" : "off"}>
              <div className="lib-main">
                <b>{p.name}</b>
                <span className="fine">{p.standard} {p.issue} · {plural(p.file_count, "file")} · {p.id}</span>
                <span className="lib-tags">
                  {p.provenance === "synthetic" ? <span className="tag warn">evaluation subset</span> : <span className="tag ok">{p.provenance}</span>}
                  {p.doc_types.map((d: Json) => <span key={d.id} className="tag" title={`root <${d.root}>`}>{d.label}</span>)}
                </span>
              </div>
              <div className="lib-actions">
                <label className="switch"><input type="checkbox" checked={p.enabled} onChange={(e) => act(api.setPackage(p.id, e.target.checked), e.target.checked ? "Enabled" : "Disabled")} /> Enabled</label>
                <a className="link" href={api.packageExportUrl(p.id)}>Export</a>
                <button className="link danger" onClick={() => confirm(`Remove ${p.name}? Documents are no longer checked against it.`) && act(api.removePackage(p.id), "Removed")}>Remove</button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Proposal({ p, onDone, onCancel }: { p: Json; onDone: () => void; onCancel: () => void }) {
  const { toast } = useStudio();
  const [c, setC] = useState<Json>(() => ({ ...p, types: p.default_types, doc_types: p.doc_types?.map((t: Json) => ({ ...t })) }));
  const [busy, setBusy] = useState(false);
  const install = async (replace = false) => {
    setBusy(true);
    try {
      const r = await api.installSchemas(p.staging, c, replace);
      toast(`Installed ${r.package.name}${r.brex_installed?.length ? ` and ${plural(r.brex_installed.length, "BREX")}` : ""}`);
      onDone();
    } catch (e) {
      const msg = (e as Error).message;
      if (/already installed/.test(msg) && confirm(`${msg}\n\nReplace the installed build?`)) { setBusy(false); return install(true); }
      toast(msg, "bad");
    } finally { setBusy(false); }
  };
  const setType = (i: number, k: string, v: unknown) => setC({ ...c, doc_types: c.doc_types.map((t: Json, j: number) => j === i ? { ...t, [k]: v } : t) });
  return (
    <div className="proposal">
      <h3>{p.kind === "package" ? "Schema package" : p.kind === "s1000d" ? "S1000D schemas" : p.kind === "dtd" ? "DTD set" : "XSD set"} found</h3>
      {(p.notes ?? []).map((n: string, i: number) => <p key={i} className="fine">{n}</p>)}
      <div className="fields">
        {p.kind !== "package" && <label><span>Issue / revision (required)</span><input value={c.issue ?? ""} onChange={(e) => setC({ ...c, issue: e.target.value })} placeholder="for example 4.2 or 2023.1" /></label>}
        {(p.kind === "dtd" || p.kind === "xsd") && <label><span>Standard</span><select value={c.standard} onChange={(e) => setC({ ...c, standard: e.target.value })}>{STANDARDS.map((s) => <option key={s}>{s}</option>)}</select></label>}
        {(p.kind === "dtd" || p.kind === "xsd") && <label className="wide"><span>Name (optional)</span><input value={c.name ?? ""} onChange={(e) => setC({ ...c, name: e.target.value })} /></label>}
        {p.kind === "s1000d" && p.folders?.length > 1 && <label className="wide"><span>Schema copy</span><select value={c.folder} onChange={(e) => setC({ ...c, folder: e.target.value })}>
          {p.folders.map((f: Json) => <option key={f.path} value={f.path}>{f.path} {f.issue ? `(issue ${f.issue})` : ""}</option>)}</select></label>}
        {p.kind === "s1000d" && <label className="wide"><span>Document types (schema names, comma separated)</span>
          <input value={(c.types ?? []).join(", ")} onChange={(e) => setC({ ...c, types: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} /></label>}
      </div>
      {(p.kind === "dtd" || p.kind === "xsd") && c.doc_types?.length > 0 && (
        <table className="grid compact static">
          <thead><tr><th /><th>Document type</th><th>File</th><th>Top element</th></tr></thead>
          <tbody>{c.doc_types.map((t: Json, i: number) => (
            <tr key={i}><td><input type="checkbox" checked={!!t.selected} onChange={(e) => setType(i, "selected", e.target.checked)} aria-label={`Install ${t.id}`} /></td>
              <td>{t.label ?? t.id}{t.problem && <div className="warn-t fine">{t.problem}</div>}</td><td className="fine">{t.schema_file}</td>
              <td>{t.root_candidates?.length ? <select value={t.root ?? ""} onChange={(e) => setType(i, "root", e.target.value)}><option value="">Choose…</option>
                {t.root_candidates.map((r: string) => <option key={r}>{r}</option>)}</select> : t.root}</td></tr>))}</tbody>
        </table>
      )}
      <div className="actions">
        <button className="btn primary" disabled={busy || (p.kind !== "package" && !c.issue)} onClick={() => install()}>{busy ? "Installing…" : "Install"}</button>
        <button className="link" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- BREX
function BrexPanel({ conf, act }: { conf: Json; act: Act }) {
  const [named, setNamed] = useState("");
  const [use, setUse] = useState("");
  const subs: Record<string, string> = conf.brex_substitutes ?? {};
  return (
    <section className="panel-sec">
      <div className="sec-head">
        <p className="fine">S1000D data modules name the BREX that holds their business rules. Add the S1000D default BREX for your issue and your project BREX (information code 022); a project BREX that builds on the default is checked as a chain. Set which BREX your documents name under Publish → Publication identity.</p>
        <label className="btn primary file-btn">Add a BREX data module
          <input type="file" accept=".xml,.XML" onChange={(e) => { const f = e.target.files?.[0]; if (f) act(api.addBrex(f), "BREX added"); e.target.value = ""; }} /></label>
      </div>
      {!conf.brex.length ? <p className="fine">No BREX installed. S1000D documents are then checked against the schema only, and the check says so.</p> : (
        <ul className="lib-list">{conf.brex.map((b: Json) => (
          <li key={b.dmc + b.issue}>
            <div className="lib-main">
              <b>DMC-{b.dmc}</b>
              <span className="fine">{b.title} · issue {b.issue}{b.schema_issue ? ` · S1000D ${b.schema_issue}` : ""}</span>
              <span className="lib-tags"><span className="tag">{plural(b.rules, "rule")}</span>{b.sns_systems > 0 && <span className="tag">SNS</span>}
                {b.parent_dmc && <span className="tag info">builds on DMC-{b.parent_dmc}</span>}</span>
            </div>
            <div className="lib-actions"><button className="link danger" onClick={() => confirm(`Remove DMC-${b.dmc}?`) && act(api.removeBrex(b.dmc, b.issue), "Removed")}>Remove</button></div>
          </li>))}</ul>
      )}
      <h2>Stand-ins</h2>
      <p className="fine">When documents name a BREX you do not have, an installed one can stand in for it. Reports always say that a stand-in was used.</p>
      {Object.entries(subs).map(([n, u]) => <p key={n}>DMC-{n} → DMC-{u} <button className="link danger" onClick={() => act(api.removeSubstitute(n), "Removed")}>Remove</button></p>)}
      {conf.brex.length > 0 && <form className="row-form" onSubmit={(e) => { e.preventDefault(); act(api.setSubstitute(named.replace(/^DMC-/i, ""), use), "Stand-in set").then((ok) => ok && setNamed("")); }}>
        <input aria-label="BREX the documents name" placeholder="BREX the documents name, e.g. S1000D-G-04-10-0301-00A-022A-D" value={named} onChange={(e) => setNamed(e.target.value)} />
        <select aria-label="Use instead" value={use} onChange={(e) => setUse(e.target.value)}><option value="">Use…</option>{conf.brex.map((b: Json) => <option key={b.dmc} value={b.dmc}>DMC-{b.dmc}</option>)}</select>
        <button className="btn" disabled={!named || !use}>Set stand-in</button>
      </form>}
    </section>
  );
}

// ---------------------------------------------------------------- applies-to editor
function AppliesTo({ value, onChange }: { value: Json; onChange: (v: Json) => void }) {
  const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
  return (
    <div className="fields applies">
      <label><span>Standards</span><input placeholder="all, or e.g. S1000D" defaultValue={(value.standards ?? []).join(", ")} onChange={(e) => onChange({ ...value, standards: list(e.target.value) })} /></label>
      <label><span>Document types</span><input placeholder="all, or e.g. proced, ipd" defaultValue={(value.doc_types ?? []).join(", ")} onChange={(e) => onChange({ ...value, doc_types: list(e.target.value) })} /></label>
      <label><span>Top elements</span><input placeholder="all, or e.g. dmodule, cmm" defaultValue={(value.roots ?? []).join(", ")} onChange={(e) => onChange({ ...value, roots: list(e.target.value) })} /></label>
    </div>
  );
}
const appliesText = (a: Json) => {
  const parts = [a?.standards?.join(", "), a?.doc_types?.length ? `types ${a.doc_types.join(", ")}` : "", a?.roots?.length ? `<${a.roots.join(">, <")}>` : ""].filter(Boolean);
  return parts.length ? parts.join(" · ") : "all documents";
};

// ---------------------------------------------------------------- rule sets
function RulesPanel({ conf, act }: { conf: Json; act: Act }) {
  const [file, setFile] = useState<File | null>(null);
  const [applies, setApplies] = useState<Json>({});
  const [k, setK] = useState(0);
  return (
    <section className="panel-sec">
      <div className="sec-head">
        <p className="fine">Your own business rules, checked with the BREX. A rule set is a Schematron file (<code>.sch</code>; <code>@id</code> is the rule id, <code>@role</code> the severity) or a rule file in YAML or JSON: each rule has an id, a <code>context</code> XPath and one test (<code>assert</code>, <code>forbid</code>, <code>allowed</code> or <code>pattern</code>). Examples are in the <code>examples/rules</code> folder of the delivery. Installing a file with the same id replaces it.</p>
      </div>
      <form key={k} className="add-box" onSubmit={(e) => { e.preventDefault(); if (file) act(api.addRules(file, applies), "Rule set added").then((ok) => { if (ok) { setFile(null); setApplies({}); setK(k + 1); } }); }}>
        <label className="btn file-btn">{file ? file.name : "Choose a rule file"}<input type="file" accept=".sch,.yaml,.yml,.json" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></label>
        <p className="fine">Applies to (leave empty for every document; a YAML file can say this itself):</p>
        <AppliesTo value={applies} onChange={setApplies} />
        <button className="btn primary" disabled={!file}>Add rule set</button>
      </form>
      {!conf.rules.length ? <p className="fine">No rule sets yet.</p> : (
        <ul className="lib-list">{conf.rules.map((r: Json) => (
          <li key={r.id} className={r.enabled ? "" : "off"}>
            <div className="lib-main">
              <b>{r.name}</b>
              <span className="fine">{r.kind === "schematron" ? "Schematron" : "XPath rules"} · {plural(r.rules, "rule")} · applies to {appliesText(r.applies_to)}</span>
            </div>
            <div className="lib-actions">
              <label className="switch"><input type="checkbox" checked={r.enabled} onChange={(e) => act(api.changeRules(r.id, { enabled: e.target.checked }), e.target.checked ? "Enabled" : "Disabled")} /> Enabled</label>
              <a className="link" href={api.rulesSourceUrl(r.id)} target="_blank" rel="noreferrer">Source</a>
              <button className="link danger" onClick={() => confirm(`Remove ${r.name}?`) && act(api.removeRules(r.id), "Removed")}>Remove</button>
            </div>
          </li>))}</ul>
      )}
    </section>
  );
}

// ---------------------------------------------------------------- stylesheets
function StylesPanel({ conf, act }: { conf: Json; act: Act }) {
  const [file, setFile] = useState<File | null>(null);
  const [kind, setKind] = useState("view");
  const [order, setOrder] = useState(100);
  const [applies, setApplies] = useState<Json>({});
  const [k, setK] = useState(0);
  const list: Json[] = conf.stylesheets;
  return (
    <section className="panel-sec">
      <div className="sec-head">
        <p className="fine">XSLT 1.0 stylesheets. A <b>view</b> turns a document into the reading view on Check documents; yours take precedence over the built-in ones for the documents they apply to. A <b>transform</b> rewrites Studio's XML before it is checked and published (house conventions, a customer's flavour of the schema); transforms run in order. A stylesheet that includes others is added as a zip. Stylesheets cannot reach the network or write files.</p>
      </div>
      <form key={k} className="add-box" onSubmit={(e) => { e.preventDefault(); if (file) act(api.addStylesheet(file, kind, applies, order), "Stylesheet added").then((ok) => { if (ok) { setFile(null); setApplies({}); setK(k + 1); } }); }}>
        <div className="toolbar">
          <label className="btn file-btn">{file ? file.name : "Choose a stylesheet"}<input type="file" accept=".xsl,.xslt,.zip" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></label>
          <div className="seg" role="radiogroup" aria-label="Kind">
            {(["view", "transform"] as const).map((x) => <button type="button" key={x} role="radio" aria-checked={kind === x} className={kind === x ? "on" : ""} onClick={() => setKind(x)}>{x === "view" ? "View" : "Transform"}</button>)}
          </div>
          {kind === "transform" && <label className="fine">Order <input type="number" value={order} onChange={(e) => setOrder(+e.target.value)} style={{ width: "5rem" }} /></label>}
        </div>
        <AppliesTo value={applies} onChange={setApplies} />
        <button className="btn primary" disabled={!file}>Add stylesheet</button>
      </form>
      <ul className="lib-list">{list.map((s: Json) => (
        <li key={s.id} className={s.enabled ? "" : "off"}>
          <div className="lib-main">
            <b>{s.name}</b>
            <span className="fine">{s.kind === "view" ? "View" : `Transform, order ${s.order}`} · applies to {appliesText(s.applies_to)}</span>
            {s.builtin && <span className="lib-tags"><span className="tag">built in</span></span>}
          </div>
          <div className="lib-actions">
            <label className="switch"><input type="checkbox" checked={s.enabled} onChange={(e) => act(api.changeStylesheet(s.id, { enabled: e.target.checked }), e.target.checked ? "Enabled" : "Disabled")} /> Enabled</label>
            <a className="link" href={api.stylesheetSourceUrl(s.id)} target="_blank" rel="noreferrer">Source</a>
            {!s.builtin && <button className="link danger" onClick={() => confirm(`Remove ${s.name}?`) && act(api.removeStylesheet(s.id), "Removed")}>Remove</button>}
          </div>
        </li>))}</ul>
    </section>
  );
}

// ---------------------------------------------------------------- try any document
function TryPanel() {
  const { toast } = useStudio();
  const [rep, setRep] = useState<Json | null>(null);
  const [name, setName] = useState("");
  return (
    <section className="panel-sec">
      <div className="sec-head">
        <p className="fine">Check any XML document against what is installed, without a run: useful when writing a new rule set or stylesheet.</p>
        <label className="btn primary file-btn">Choose an XML document
          <input type="file" accept=".xml,.XML,.sgm" onChange={async (e) => {
            const f = e.target.files?.[0]; e.target.value = "";
            if (!f) return;
            setName(f.name);
            try { setRep(await api.checkAny(f)); } catch (err) { toast((err as Error).message, "bad"); }
          }} /></label>
      </div>
      {rep && <>
        <h2>{name}</h2>
        <p className="fine">{rep.identification.package_id ? `Validated against ${rep.identification.package_id} · ${rep.identification.doc_type} (${rep.identification.how}).` : rep.identification.notes?.join(" ")}</p>
        <ol className="stage-strip">{rep.stages.map((st: Json) => <li key={st.stage} className={st.status === "passed" ? "ok" : st.status === "failed" ? "bad" : ""} title={st.note}>
          <span className="stage-n">{st.stage}</span><span className="stage-t">{st.name}</span><span className="stage-s">{st.status}</span>{st.note && <span className="stage-note">{st.note}</span>}</li>)}</ol>
        <Diagnostics items={rep.diagnostics} />
        {rep.view?.html && <iframe className="doc-frame" title={`Reading view of ${name}`} sandbox="" srcDoc={rep.view.html} />}
      </>}
    </section>
  );
}
