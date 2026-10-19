import { useEffect, useState } from "react";
import { api, Json } from "../api";
import { plural, useStudio } from "../state";

/* Publishing: pick the outputs and where they go, set the publication identity once per product.
   S1000D and ATA documents are checked again first; documents with errors are held back unless the
   writer publishes them anyway, and the check result travels with every published file. */

// defaults the backend uses when a field is empty (publish/config.py PublicationConfig)
const DEFAULTS: Record<string, string> = { model_ident_code: "TECHPUB", system_diff_code: "A", system_code: "32", sub_system_code: "4",
  sub_sub_system_code: "2", assy_code: "11", disassy_code: "00", issue_number: "001", in_work: "01", ata_chapter: "32", ata_section: "42",
  ata_subject: "11", cmm_doc_number: "top part number", enterprise_code: "CAGE of the top part", brex_dmc: "S1000D-G-04-10-0301-00A-022A-D" };
const ID_FIELDS: { k: string; label: string; group: "s1000d" | "ata" | "both"; w?: number }[] = [
  { k: "model_ident_code", label: "Model ident code", group: "s1000d", w: 2 },
  { k: "system_diff_code", label: "System diff.", group: "s1000d" },
  { k: "system_code", label: "System", group: "s1000d" },
  { k: "sub_system_code", label: "Sub-system", group: "s1000d" },
  { k: "sub_sub_system_code", label: "Sub-sub-system", group: "s1000d" },
  { k: "assy_code", label: "Assembly", group: "s1000d" },
  { k: "disassy_code", label: "Disassembly", group: "s1000d" },
  { k: "issue_number", label: "Issue", group: "s1000d" },
  { k: "in_work", label: "In work", group: "s1000d" },
  { k: "brex_dmc", label: "BREX data module code (the BREX the data modules name)", group: "s1000d", w: 4 },
  { k: "ata_chapter", label: "ATA chapter", group: "ata" },
  { k: "ata_section", label: "Section", group: "ata" },
  { k: "ata_subject", label: "Subject", group: "ata" },
  { k: "cmm_doc_number", label: "CMM number", group: "ata", w: 2 },
  { k: "enterprise_code", label: "CAGE / enterprise code", group: "both" },
  { k: "enterprise_name", label: "Enterprise name", group: "both", w: 2 },
];

export function PublishView() {
  const { run, project, system, toast, selectProject, setView, touch } = useStudio();
  const targets: Record<string, string> = system?.targets ?? {};
  const conns: Json[] = system?.connectors_status ?? [];
  const [connector, setConnector] = useState<string>("");
  const [chosen, setChosen] = useState<Set<string>>(new Set(["s1000d", "ata"]));
  const [ident, setIdent] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<Json[]>([]);
  const [blocked, setBlocked] = useState<string | null>(null);

  useEffect(() => { if (!connector && conns.length) setConnector((conns.find((c) => c.available) ?? conns[0]).name); }, [conns, connector]);
  useEffect(() => { setIdent({ ...(project?.settings?.publication ?? {}) }); }, [project]);
  const loadHistory = () => run && api.publications(run.id).then(setHistory).catch(() => setHistory([]));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { loadHistory(); }, [run?.id]);

  if (!run || !project) return null;
  const conn = conns.find((c) => c.name === connector);
  const ready = run.status === "done";
  const chk = run.stages?.check?.summary;

  const saveIdent = async () => {
    try {
      await api.saveSettings(project.id, { publication: ident });
      await selectProject(project.id);
      if (!run.busy) await api.checkAgain(run.id).catch(() => undefined);
      touch();
      toast("Publication identity saved; documents checked again");
    } catch (e) { toast((e as Error).message, "bad"); }
  };
  const publish = async (allowErrors = false) => {
    setBusy(true); setBlocked(null);
    try {
      await api.saveSettings(project.id, { publication: ident });
      const r = await api.publish(run.id, { connector, targets: [...chosen], publication: ident, allow_errors: allowErrors });
      const docs: Json[] = r.documents ?? [];
      toast(r.published_with_errors ? `Published ${plural(docs.length, "file")}, with validation errors recorded` : `Published ${plural(docs.length, "file")}`,
        r.published_with_errors ? "bad" : "ok");
      loadHistory(); touch();
    } catch (e) {
      const err = e as Error & { status?: number };
      if (err.status === 409) setBlocked(err.message);
      else toast(`Not published: ${err.message}`, "bad");
    } finally { setBusy(false); }
  };

  return (
    <div className="view publish">
      <header className="view-head">
        <h1>Publish</h1>
        <p className="lede-s">{run.signed_off ? "This run is the signed-off baseline." : "You can publish a working run for review; sign it off to make it the baseline."} S1000D and ATA documents are checked again just before they leave, and what was checked is exactly what is published.</p>
        {chk && <p className={chk.errors ? "callout bad" : "callout info"}>
          Last check: {chk.passed} of {plural(chk.documents, "document")} pass{chk.errors ? `, ${plural(chk.errors, "error")}` : ""}{chk.warnings ? `, ${plural(chk.warnings, "warning")}` : ""}.
          <button className="link" onClick={() => setView("check")}>See the check</button></p>}
      </header>

      <div className="pub-grid">
        <section className="pub-block">
          <h2>Outputs</h2>
          {Object.entries(targets).map(([k, label]) => (
            <label key={k} className="check">
              <input type="checkbox" checked={chosen.has(k)} onChange={(e) => { const n = new Set(chosen); if (e.target.checked) n.add(k); else n.delete(k); setChosen(n); }} />
              <span>{label}{k !== "techpub" && <span className="fine"> · validated</span>}</span>
              <span className="check-actions"><a href={api.exportUrl(run.id, k)} className="link">Download</a></span>
            </label>
          ))}
        </section>

        <section className="pub-block">
          <h2>Destination</h2>
          {conns.map((c) => (
            <label key={c.name} className={`check dest ${c.available ? "" : "off"}`}>
              <input type="radio" name="conn" checked={connector === c.name} onChange={() => setConnector(c.name)} />
              <span><b>{c.name === "folder" ? "Folder" : c.name}</b> <span className="fine">{c.path ?? c.url ?? c.error ?? ""}</span></span>
            </label>
          ))}
          <p className="fine">Other destinations (PLM, CMS, document control) are added in studio.yaml without changing Studio.</p>
          {blocked && <div className="callout bad">{blocked}
            <button className="btn" onClick={() => setView("check")}>See the problems</button>
            <button className="btn quiet" onClick={() => publish(true)}>Publish anyway</button></div>}
          <button className="btn primary big" disabled={!ready || busy || !chosen.size || !conn?.available} onClick={() => publish(false)}>
            {busy ? "Checking and publishing…" : `Publish to ${connector === "folder" ? "the folder" : connector}`}</button>
        </section>

        <section className="pub-block ident">
          <h2>Publication identity</h2>
          <p className="fine">Set once per product. Empty fields take the defaults shown. Saving checks the documents again.</p>
          {(["s1000d", "ata", "both"] as const).map((g) => (
            <div key={g} className="fields">
              <h3>{g === "s1000d" ? "S1000D data module code" : g === "ata" ? "ATA iSpec 2200" : "Company"}</h3>
              {ID_FIELDS.filter((f) => f.group === g).map((f) => (
                <label key={f.k} className={f.w === 2 ? "wide2" : f.w === 4 ? "wide" : ""}><span>{f.label}</span>
                  <input value={ident[f.k] ?? ""} placeholder={DEFAULTS[f.k] ?? ""} onChange={(e) => setIdent({ ...ident, [f.k]: e.target.value })} /></label>))}
            </div>
          ))}
          <button className="btn" onClick={saveIdent}>Save identity</button>
        </section>
      </div>

      {history.length > 0 && (
        <section className="pub-history">
          <h2>Published</h2>
          {[...history].reverse().map((h, i) => (
            <div key={i} className="pub-run">
              <div className="pub-run-head"><b>{h.connector === "folder" ? "Folder" : h.connector ?? "—"}</b> <span className="fine">{h.at ? new Date(h.at).toLocaleString() : ""}{h.location ? ` · ${h.location}` : ""}</span>
                {h.published_with_errors && <span className="tag bad">published with validation errors</span>}</div>
              <table className="grid compact static">
                <thead><tr><th>File</th><th>Result</th><th>Check</th></tr></thead>
                <tbody>{(h.documents ?? []).map((d: Json, k: number) => (
                  <tr key={k}>
                    <td className="pn">{d.file}</td>
                    <td>{d.action === "written" ? "Written" : d.action ?? ""}{d.error && <span className="bad-t"> {d.error}</span>}</td>
                    <td>{d.statuses ? <>
                      <span className={`tag ${d.passed ? "ok" : "bad"}`}>{d.passed ? "Valid" : "Errors"}</span>
                      <span className="fine"> schema {d.statuses.structural}, business rules {d.statuses.business_rules}{d.counts?.warning ? `, ${plural(d.counts.warning, "warning")}` : ""}</span></> :
                      <span className="fine">not an XML publication</span>}</td>
                  </tr>))}</tbody>
              </table>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
