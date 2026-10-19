import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Json } from "../../api";

/* The document view: the XML as a document, with its text editable in place. With "Tags" on, every element
   is drawn as a bracket with its name (and attributes) on a tab — the same tag view as ASTHRA's editor.
   Problems from the check are tied to their element: red for errors, amber for warnings.

   The XML text stays the single source: editing here changes the parsed document and writes it back as
   text; changes made in the XML view are parsed again. */

const SEV_RANK: Record<string, number> = { fatal: 0, error: 0, warning: 1, info: 2 };
const isErr = (d: Json) => d.severity === "error" || d.severity === "fatal";

/* parts of S1000D / ATA documents shown folded in the document view (they are metadata, not reading text) */
const FOLDED = new Set(["identAndStatusSection", "idstatus", "dmAddressItems", "dmStatus"]);
const PROSE = new Set(["title", "para", "simplePara", "warningAndCautionPara", "notePara", "attentionListItemPara", "techName", "infoName"]);
const FOLD_LABEL: Record<string, string> = { identAndStatusSection: "Identification and status", idstatus: "Identification and status" };

/** Line of each element's start tag, in document order (the order of getElementsByTagName("*")). */
export function elementLines(text: string): number[] {
  const re = /<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\?[\s\S]*?\?>|<!DOCTYPE(?:[^[>]|\[[\s\S]*?\])*>|<([A-Za-z_][\w.:-]*)/g;
  const out: number[] = [];
  let line = 1, pos = 0, m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    for (let i = pos; i < m.index; i++) if (text.charCodeAt(i) === 10) line++;
    pos = m.index;
    if (m[1]) out.push(line);
  }
  return out;
}

/** '/dmodule/content/procedure/mainProcedure/proceduralStep[2]' — the path the checker reports. */
export function readablePath(el: Element): string {
  const parts: string[] = [];
  let e: Element | null = el;
  while (e) {
    const p: Element | null = e.parentElement;
    const name = e.localName;
    if (!p) parts.push(name);
    else {
      const same = Array.from(p.children).filter((c) => c.localName === e!.localName && c.namespaceURI === e!.namespaceURI);
      parts.push(same.length > 1 ? `${name}[${same.indexOf(e) + 1}]` : name);
    }
    e = p;
  }
  return "/" + parts.reverse().join("/");
}

interface Parsed { doc: Document; prolog: string; tail: string }

function parse(text: string): Parsed | { error: string } {
  const doc = new DOMParser().parseFromString(text, "application/xml");
  const err = doc.getElementsByTagName("parsererror")[0];
  if (err) return { error: (err.textContent || "The XML is not well-formed.").replace(/\s+/g, " ").trim() };
  const start = text.search(new RegExp(`<${doc.documentElement.nodeName}[\\s/>]`));
  const endTag = `</${doc.documentElement.nodeName}>`;
  const end = text.lastIndexOf(endTag);
  return { doc, prolog: start > 0 ? text.slice(0, start) : "",
           tail: end >= 0 ? text.slice(end + endTag.length) : "\n" };
}

function serialize(p: Parsed): string {
  return p.prolog + new XMLSerializer().serializeToString(p.doc.documentElement) + p.tail;
}

interface Ctx {
  tags: boolean; readOnly: boolean; sel: Element | null; select: (e: Element) => void;
  diags: Map<Element, Json[]>; edited: () => void; key: (n: Node) => number; skip: Set<Element>;
}

export function XmlDoc({ text, onChange, readOnly, tags, diagnostics, onLine }: {
  text: string; onChange: (t: string) => void; readOnly: boolean; tags: boolean; diagnostics: Json[]; onLine: (l: number) => void;
}) {
  const [parsed, setParsed] = useState<Parsed | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rev, setRev] = useState(0);
  const [sel, setSel] = useState<Element | null>(null);
  const emitted = useRef<string | null>(null);
  const ids = useRef(new WeakMap<Node, number>());
  const next = useRef(1);

  useEffect(() => {
    if (text === emitted.current) return;          // our own edit coming back
    const p = parse(text);
    if ("error" in p) { setError(p.error); return; }   // keep the last good tree, read-only, until the XML is fixed
    setError(null); setParsed(p); setSel(null);
    emitted.current = text;
  }, [text]);

  const edited = () => {
    if (!parsed) return;
    const t = serialize(parsed);
    emitted.current = t;
    onChange(t);
  };
  const structural = () => { edited(); setRev((r) => r + 1); };

  const diags = useMemo(() => {
    const m = new Map<Element, Json[]>();
    if (!parsed || error) return m;
    const els = Array.from(parsed.doc.getElementsByTagName("*"));
    const lines = elementLines(text);
    const byPath = new Map<string, Element>();
    els.forEach((e) => byPath.set(readablePath(e), e));
    for (const d of diagnostics) {
      let el: Element | undefined = d.element_path ? byPath.get(d.element_path) : undefined;
      if (!el && d.line && lines.length === els.length) {
        let lo = 0, hi = lines.length - 1, hit = -1;
        while (lo <= hi) { const mid = (lo + hi) >> 1; if (lines[mid] <= d.line) { hit = mid; lo = mid + 1; } else hi = mid - 1; }
        if (hit >= 0) el = els[hit];
      }
      if (el) { if (!m.has(el)) m.set(el, []); m.get(el)!.push(d); }
    }
    return m;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parsed, diagnostics, text, rev, error]);

  if (!parsed) return <div className="callout bad">{error ?? "Reading the document…"}</div>;
  const ro = readOnly || !!error;
  const ctx: Ctx = {
    tags, readOnly: ro, sel, select: setSel, diags, edited, skip: new Set(),
    key: (n) => { let k = ids.current.get(n); if (!k) { k = next.current++; ids.current.set(n, k); } return k; },
  };
  const lineOf = (el: Element) => {
    const els = Array.from(parsed.doc.getElementsByTagName("*"));
    const lines = elementLines(text);
    return lines.length === els.length ? lines[els.indexOf(el)] : undefined;
  };

  return (
    <div className="xdoc-wrap">
      <div className="xdoc-main">
        {error && <div className="callout bad xdoc-banner">The XML is not well-formed, so the document shows the last good version and cannot be edited here.
          <span className="fine"> {error}</span></div>}
        <div className={`xdoc ${tags ? "mode-tags" : "mode-doc"}`} data-rev={rev}>
          <Block el={parsed.doc.documentElement} ctx={ctx} />
        </div>
      </div>
      {sel && sel.ownerDocument === parsed.doc && parsed.doc.contains(sel) ? (
        <Inspector key={ctx.key(sel)} el={sel} ctx={ctx} line={lineOf(sel)} onLine={onLine} onStructure={structural}
          onClose={() => setSel(null)} problems={diags.get(sel) ?? []} />
      ) : (
        <aside className="x-inspector empty"><p className="fine">Select an element to see its attributes and problems
          {ro ? "." : ", change its attributes, move, duplicate or delete it."} Text is edited where it is.</p></aside>
      )}
    </div>
  );
}

const hasText = (el: Element) => Array.from(el.childNodes).some((n) => n.nodeType === Node.TEXT_NODE && n.textContent!.trim() !== "")
  || Array.from(el.childNodes).some((n) => n.nodeType === Node.CDATA_SECTION_NODE);
const attrSummary = (el: Element, max = 3) => Array.from(el.attributes).filter((a) => !a.name.startsWith("xmlns") && !a.name.startsWith("xsi:"))
  .slice(0, max).map((a) => `${a.name}="${a.value.length > 24 ? a.value.slice(0, 22) + "…" : a.value}"`).join(" ");

const humanize = (n: string) => { const t = n.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase(); return t.charAt(0).toUpperCase() + t.slice(1); };

/** The leaf fields of a small record-like element (all short values, no prose), or null. */
function recordLeaves(el: Element): Element[] | null {
  if (FOLDED.has(el.localName) || el.children.length < 2) return null;
  const all = Array.from(el.getElementsByTagName("*"));
  if (all.length > 14 || all.some((c) => PROSE.has(c.localName))) return null;
  const leaves = all.filter((c) => c.children.length === 0);
  if (leaves.length < 2 || leaves.length > 8) return null;
  if (leaves.some((c) => (c.textContent ?? "").trim().length > 40)) return null;
  // no loose text outside the leaves
  if (all.concat(el).some((c) => c.children.length > 0 && hasText(c))) return null;
  return leaves;
}

/** 'DMC-TECHPUB-A-32-42-11-00A-530A-C', as the reading view shows it. */
function dmcOf(el: Element): string {
  const c = el.getElementsByTagName("dmCode")[0];
  if (!c) return "";
  const a = (n: string) => c.getAttribute(n) ?? "";
  return `DMC-${a("modelIdentCode")}-${a("systemDiffCode")}-${a("systemCode")}-${a("subSystemCode")}${a("subSubSystemCode")}-` +
    `${a("assyCode")}-${a("disassyCode")}${a("disassyCodeVariant")}-${a("infoCode")}${a("infoCodeVariant")}-${a("itemLocationCode")}`;
}
function metaOf(el: Element): string {
  const i = el.getElementsByTagName("issueInfo")[0], d = el.getElementsByTagName("issueDate")[0];
  const parts = [];
  if (i) parts.push(`Issue ${i.getAttribute("issueNumber")}-${i.getAttribute("inWork")}`);
  if (d) parts.push(`${d.getAttribute("year")}-${d.getAttribute("month")}-${d.getAttribute("day")}`);
  return parts.join("    ");
}

function nodeClass(el: Element, ctx: Ctx, base: string) {
  const ds = ctx.diags.get(el);
  const sev = ds ? (ds.some(isErr) ? "has-error" : ds.some((d) => d.severity === "warning") ? "has-warning" : "") : "";
  return [base, sev, ctx.sel === el ? "is-selected" : ""].filter(Boolean).join(" ");
}
const tip = (el: Element, ctx: Ctx) => ctx.diags.get(el)?.sort((a, b) => SEV_RANK[a.severity] - SEV_RANK[b.severity])
  .map((d) => `${d.rule_id}: ${d.message}`).join("\n");

function Label({ el, ctx }: { el: Element; ctx: Ctx }) {
  if (!ctx.tags) return null;
  const s = attrSummary(el);
  return <span className="x-label" onClick={(e) => { e.stopPropagation(); ctx.select(el); }}>{el.nodeName}{s && <span className="x-attrs"> {s}</span>}</span>;
}

function Block({ el, ctx, force }: { el: Element; ctx: Ctx; force?: boolean }) {
  if (!force && ctx.skip.has(el)) return null;
  const kids = Array.from(el.childNodes);
  const name = el.localName;
  const select = (e: React.MouseEvent) => { e.stopPropagation(); ctx.select(el); };
  if (!kids.some((n) => n.nodeType === Node.ELEMENT_NODE) && !hasText(el)) {
    return (
      <span className={nodeClass(el, ctx, "x-node x-atom")} data-name={name} title={tip(el, ctx)} onClick={select}>
        <span className="x-label">{el.nodeName}</span>{attrSummary(el, 4) && <span className="x-summary">{attrSummary(el, 4)}</span>}
      </span>
    );
  }
  if (hasText(el)) {
    return (
      <div className={nodeClass(el, ctx, "x-node x-text")} data-name={name} title={tip(el, ctx)} onClick={select}>
        <Label el={el} ctx={ctx} />
        <span className="x-content"><Inline nodes={kids} ctx={ctx} /></span>
      </div>
    );
  }
  const leaves = recordLeaves(el);
  if (!ctx.tags && leaves) {
    // a record (spare part, supply, tool: name, code, part number, quantity): one line of labelled fields
    return (
      <div className={nodeClass(el, ctx, "x-node x-record")} data-name={name} title={tip(el, ctx)} onClick={select}>
        {leaves.map((c) => (
          <span key={ctx.key(c)} className={nodeClass(c, ctx, "x-node x-field")} data-name={c.localName} title={tip(c, ctx)}
            onClick={(e) => { e.stopPropagation(); ctx.select(c); }}>
            <span className="x-cap">{humanize(c.localName)}</span>
            {hasText(c) ? <Inline nodes={Array.from(c.childNodes)} ctx={ctx} /> : <span className="x-summary">{attrSummary(c, 3) || "—"}</span>}
          </span>
        ))}
      </div>
    );
  }
  const body = kids.map((n) => n.nodeType === Node.ELEMENT_NODE ? <Block key={ctx.key(n)} el={n as Element} ctx={ctx} />
    : n.nodeType === Node.COMMENT_NODE && ctx.tags ? <div key={ctx.key(n)} className="x-comment">{`<!-- ${n.textContent?.trim()} -->`}</div> : null);
  if (!ctx.tags && FOLDED.has(name)) {
    // the title reads as the document's heading; the rest of the identification is folded
    const title = el.getElementsByTagName("dmTitle")[0] ?? null;
    if (title) ctx.skip.add(title);
    return (
      <>
      {title && <header className="x-doc-title">
        {dmcOf(el) && <p className="x-dmc">{dmcOf(el)}</p>}
        <Block el={title} ctx={ctx} force />
        {metaOf(el) && <p className="x-meta">{metaOf(el)}</p>}
      </header>}
      <details className={nodeClass(el, ctx, "x-node x-fold")} data-name={name} title={tip(el, ctx)}>
        <summary>{FOLD_LABEL[name] ?? name}{ctx.diags.get(el) || Array.from(ctx.diags.keys()).some((k) => el.contains(k)) ? <span className="x-fold-flag"> · has problems</span> : null}</summary>
        {body}
      </details>
      </>
    );
  }
  return (
    <div className={nodeClass(el, ctx, "x-node x-block")} data-name={name} title={tip(el, ctx)} onClick={select}>
      <Label el={el} ctx={ctx} />
      {body}
    </div>
  );
}

function Inline({ nodes, ctx }: { nodes: ChildNode[]; ctx: Ctx }) {
  return (
    <>
      {nodes.map((n) => {
        if (n.nodeType === Node.TEXT_NODE || n.nodeType === Node.CDATA_SECTION_NODE) {
          return n.textContent!.trim() === "" ? <Fragment key={ctx.key(n)}>{" "}</Fragment>
            : <EditableText key={ctx.key(n)} node={n as CharacterData} ctx={ctx} />;
        }
        if (n.nodeType === Node.ELEMENT_NODE) {
          const el = n as Element;
          const kids = Array.from(el.childNodes);
          return (
            <span key={ctx.key(n)} className={nodeClass(el, ctx, "x-node x-inl")} data-name={el.localName} title={tip(el, ctx)}
              onClick={(e) => { e.stopPropagation(); ctx.select(el); }}>
              {ctx.tags && <span className="x-inl-tag">{el.nodeName}</span>}
              {kids.length ? <Inline nodes={kids} ctx={ctx} /> : <span className="x-inl-empty">{attrSummary(el, 2) || el.nodeName}</span>}
            </span>
          );
        }
        return null;
      })}
    </>
  );
}

function EditableText({ node, ctx }: { node: CharacterData; ctx: Ctx }) {
  const ref = useRef<HTMLSpanElement>(null);
  useLayoutEffect(() => {
    if (ref.current && document.activeElement !== ref.current && ref.current.textContent !== node.data) ref.current.textContent = node.data;
  });
  return (
    <span ref={ref} className="x-t" contentEditable={ctx.readOnly ? false : "plaintext-only"} suppressContentEditableWarning spellCheck
      onKeyDown={(e) => { if (e.key === "Enter") e.preventDefault(); }}
      onInput={(e) => { node.data = e.currentTarget.textContent ?? ""; ctx.edited(); }} />
  );
}

function Inspector({ el, ctx, line, onLine, onStructure, onClose, problems }: {
  el: Element; ctx: Ctx; line?: number; onLine: (l: number) => void; onStructure: () => void; onClose: () => void; problems: Json[];
}) {
  const [, force] = useState(0);
  const [newName, setNewName] = useState("");
  const [newValue, setNewValue] = useState("");
  const attrs = Array.from(el.attributes);
  const isRoot = !el.parentElement;
  const setAttr = (name: string, value: string) => { el.setAttribute(name, value); ctx.edited(); force((x) => x + 1); };
  const removeAttr = (name: string) => { el.removeAttribute(name); onStructure(); };
  const addAttr = () => {
    const n = newName.trim();
    if (!/^[A-Za-z_][\w.:-]*$/.test(n)) return;
    el.setAttribute(n, newValue); setNewName(""); setNewValue(""); onStructure();
  };
  const duplicate = () => { el.parentNode!.insertBefore(el.cloneNode(true), el.nextSibling); onStructure(); };
  const remove = () => { onClose(); el.parentNode!.removeChild(el); onStructure(); };
  const up = () => { const p = el.previousElementSibling; if (p) { el.parentNode!.insertBefore(el, p); onStructure(); } };
  const down = () => { const n = el.nextElementSibling; if (n) { el.parentNode!.insertBefore(n, el); onStructure(); } };

  return (
    <aside className="x-inspector" aria-label="Element">
      <div className="x-insp-head">
        <h3><code>{el.nodeName}</code></h3>
        <button className="link" onClick={onClose} aria-label="Close">Close</button>
      </div>
      <p className="x-path" title={readablePath(el)}>{readablePath(el)}</p>
      {line && <button className="link" onClick={() => onLine(line)}>Show in XML, line {line}</button>}
      {problems.length > 0 && <div className="x-insp-diags">
        {problems.map((d, i) => (
          <div key={i} className={`diag-card sev-${isErr(d) ? "error" : d.severity}`}>
            <strong>{d.rule_id}</strong> {d.message}{d.suggestion && <div className="fine">{d.suggestion}</div>}
          </div>
        ))}
      </div>}
      <h4>Attributes</h4>
      {attrs.length === 0 && <p className="fine">None.</p>}
      <table className="x-attrs-t"><tbody>
        {attrs.map((a) => (
          <tr key={a.name}>
            <th scope="row">{a.name}</th>
            <td><input value={a.value} readOnly={ctx.readOnly} onChange={(e) => setAttr(a.name, e.target.value)} aria-label={a.name} /></td>
            <td>{!ctx.readOnly && !a.name.startsWith("xmlns") && <button className="link" onClick={() => removeAttr(a.name)} aria-label={`Remove ${a.name}`}>Remove</button>}</td>
          </tr>
        ))}
      </tbody></table>
      {!ctx.readOnly && <div className="x-add-attr">
        <input placeholder="name" value={newName} onChange={(e) => setNewName(e.target.value)} aria-label="New attribute name" />
        <input placeholder="value" value={newValue} onChange={(e) => setNewValue(e.target.value)} aria-label="New attribute value"
          onKeyDown={(e) => { if (e.key === "Enter") addAttr(); }} />
        <button className="btn" onClick={addAttr} disabled={!newName.trim()}>Add</button>
      </div>}
      {!ctx.readOnly && !isRoot && <>
        <h4>Element</h4>
        <div className="x-insp-actions">
          <button className="btn" onClick={up} disabled={!el.previousElementSibling}>Move up</button>
          <button className="btn" onClick={down} disabled={!el.nextElementSibling}>Move down</button>
          <button className="btn" onClick={duplicate}>Duplicate</button>
          <button className="btn danger" onClick={remove}>Delete</button>
        </div>
        <p className="fine">The check runs again as you edit; the schema says what may go where.</p>
      </>}
    </aside>
  );
}
