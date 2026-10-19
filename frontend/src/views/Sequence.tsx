import { useEffect, useMemo, useState } from "react";
import { api, Json } from "../api";
import { useArtifact, useStudio } from "../state";

/* The disassembly sequence the planner found from the 3D contacts. The scrubber removes parts step by step in
   the viewer; the writer can reorder steps (the backend checks the blocking constraints and rebuilds the rest). */

export function SequenceView() {
  const { data: seq } = useArtifact<Json>("sequence");
  const { data: ipl } = useArtifact<Json>("ipl");
  const { run, setScene, pickHandler, editable, toast, touch } = useStudio();
  const [order, setOrder] = useState<string[] | null>(null);       // draft order while editing
  const [cur, setCur] = useState(0);                                  // 0 = assembled, n = after step n
  const [explode, setExplode] = useState(0);
  const [focus, setFocus] = useState<string | null>(null);

  const steps: Json[] = useMemo(() => {
    const ds: Json[] = seq?.disassembly ?? [];
    if (!order) return ds;
    const by = new Map(ds.map((s) => [s.unit, s]));
    return order.map((u) => by.get(u)!).filter(Boolean);
  }, [seq, order]);

  const rowByOcc = useMemo(() => {
    const m = new Map<string, Json>();
    (ipl?.rows ?? []).forEach((r: Json) => r.occ_ids?.forEach((o: string) => m.set(o, r)));
    return m;
  }, [ipl]);
  const rowById = useMemo(() => new Map<string, Json>((ipl?.rows ?? []).map((r: Json) => [r.id, r])), [ipl]);
  /** A sub-assembly that moves as one: the parts-list row that holds all its parts. */
  const holder = (s: Json): Json | null => {
    const rs = (s.members ?? []).map((m: string) => rowByOcc.get(m)).filter(Boolean) as Json[];
    if (rs.length < 2) return null;
    const chain = (r: Json) => { const out: string[] = []; let x: Json | undefined = r; while (x) { out.push(x.id); x = x.parent_row ? rowById.get(x.parent_row) : undefined; } return out; };
    // the row that holds most of the parts (a few parts of a sub-assembly may be listed elsewhere, e.g. its screws)
    const cover = new Map<string, number>();
    rs.forEach((r) => chain(r).forEach((id) => cover.set(id, (cover.get(id) ?? 0) + 1)));
    let best: Json | null = null, n = 0;
    cover.forEach((c, id) => {
      const r = rowById.get(id);
      if (r && r.depth >= 1 && (c > n || (c === n && best && r.depth > best.depth))) { best = r; n = c; }
    });
    return n >= rs.length / 2 ? best : null;
  };
  const name = (s: Json) => {
    const r = holder(s) ?? rowByOcc.get(s.unit) ?? rowByOcc.get(s.members?.[0]);
    return r ? { item: r.item, text: r.nomenclature } : { item: "", text: s.unit.replace(/_\d+$/, "") };
  };
  const partName = (unit: string) => {
    const s = (seq?.disassembly ?? []).find((x: Json) => x.unit === unit);
    const n = s ? name(s) : null;
    if (n) return n.item ? `${n.text.toLowerCase()} (${n.item})` : n.text;
    const r = rowByOcc.get(unit);
    return r ? `${r.nomenclature.toLowerCase()} (${r.item})` : "the base unit";
  };
  const blockers = useMemo(() => {
    const m = new Map<string, string[]>();
    (seq?.precedence ?? []).forEach((p: Json) => { if (!m.has(p.then)) m.set(p.then, []); m.get(p.then)!.push(p.first); });
    return m;
  }, [seq]);
  const pos = new Map(steps.map((s, i) => [s.unit, i]));
  const broken = new Set<string>();
  steps.forEach((s, i) => (blockers.get(s.unit) ?? []).forEach((b) => { if ((pos.get(b) ?? -1) > i) broken.add(s.unit); }));

  // viewer
  useEffect(() => {
    if (!seq) return;
    const hidden = new Set<string>();
    steps.slice(0, cur).forEach((s) => [...s.members, ...(s.riders ?? [])].forEach((m: string) => hidden.add(m)));
    const active = focus ? steps.find((s) => s.unit === focus) : steps[cur] ?? null;
    const sel = new Set<string>(active ? [...active.members, ...(active.riders ?? [])] : []);
    const ex = new Map<string, [number, number, number]>();
    steps.forEach((s) => [...s.members, ...(s.riders ?? [])].forEach((m: string) => ex.set(m, s.direction)));
    setScene({ selected: sel, hidden, explode: ex, explodeAmount: explode, tint: undefined, dim: null });
  }, [seq, steps, cur, explode, focus, setScene]);
  useEffect(() => () => setScene({ hidden: undefined, explode: undefined, explodeAmount: 0 }), [setScene]);

  useEffect(() => {
    pickHandler.current = (id) => {
      const s = id ? steps.find((x) => x.members.includes(id) || x.riders?.includes(id)) : null;
      setFocus(s?.unit ?? null);
      if (s) document.getElementById(`seq-${s.unit}`)?.scrollIntoView({ block: "nearest" });
    };
    return () => { pickHandler.current = null; };
  }, [steps, pickHandler]);

  // identical parts removed one after the other (four screws of the same item, same part, same direction) show as one row
  const groups = useMemo(() => {
    const out: { index: number; start: number; steps: Json[] }[] = [];
    steps.forEach((s, i) => {
      const g = out[out.length - 1];
      const p = g?.steps[g.steps.length - 1];
      const same = p && name(p).item && name(p).item === name(s).item && p.attaches === s.attaches
        && p.direction_label === s.direction_label && !(s.riders?.length) && !(p.riders?.length);
      if (same) g.steps.push(s); else out.push({ index: out.length, start: i, steps: [s] });
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [steps, rowByOcc]);
  const moveGroup = (gi: number, d: number) => {
    const j = gi + d;
    if (j < 0 || j >= groups.length) return;
    const gs = groups.map((g) => g.steps.map((s) => s.unit));
    [gs[gi], gs[j]] = [gs[j], gs[gi]];
    setOrder(gs.flat());
  };
  const save = async () => {
    if (!run || !order) return;
    try {
      const r = await api.reorder(run.id, order);
      setOrder(null);
      toast(r.warnings?.length ? `Saved with ${r.warnings.length} blocking warnings` : "Order saved. The parts list and procedures are being updated.", r.warnings?.length ? "bad" : "ok");
      touch();
    } catch (e) { toast((e as Error).message, "bad"); }
  };

  if (!seq) return <div className="view"><p className="fine">Loading the sequence…</p></div>;
  const warnings: string[] = seq.order_warnings ?? [];

  return (
    <div className="view sequence">
      <header className="view-head">
        <h1>Disassembly sequence</h1>
        <p className="lede-s">{steps.length} removal steps worked out from contacts and blocking directions in the 3D model, part by part: each part comes off right after its attaching parts.
          {seq.conflicts?.length ? ` ${seq.conflicts.length} parts could not be freed and were forced.` : " Every part comes free."}
          {seq.writer_order ? " The order was changed by a writer." : ""}</p>
        <div className="scrub">
          <label htmlFor="scrub">After step</label>
          <input id="scrub" type="range" min={0} max={steps.length} value={cur} onChange={(e) => { setCur(+e.target.value); setFocus(null); }} />
          <output>{cur === 0 ? "Assembled" : `${cur} of ${steps.length}`}</output>
          <label htmlFor="explode">Explode</label>
          <input id="explode" type="range" min={0} max={1} step={0.02} value={explode} onChange={(e) => setExplode(+e.target.value)} />
        </div>
      </header>
      {warnings.length > 0 && <div className="callout warn"><b>The saved order breaks blocking constraints:</b><ul>{warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></div>}
      {order && <div className="callout info editing">
        <span>You changed the order.{broken.size ? ` ${broken.size} steps now come before a part that blocks them.` : ""}</span>
        <button className="btn primary" onClick={save}>Save order</button>
        <button className="link" onClick={() => setOrder(null)}>Discard</button>
      </div>}
      <ol className="seq-list">
        {groups.map((g) => {
          const s = g.steps[0], i = g.start, last = g.start + g.steps.length - 1;
          const n = name(s);
          const units = g.steps.map((x) => x.unit);
          const isCur = cur >= i && cur <= last && !focus, isFocus = !!focus && units.includes(focus), done = last < cur;
          const isBroken = units.some((u) => broken.has(u)), forced = g.steps.some((x) => x.forced);
          return (
            <li key={s.unit} id={`seq-${s.unit}`} className={[isCur && "cur", isFocus && "focus", done && "done", isBroken && "broken", forced && "forced",
              s.attaches && "att", s.attached_by?.length && "held"].filter(Boolean).join(" ")}
              onClick={() => { setFocus(isFocus ? null : s.unit); }}>
              <span className="seq-no">{g.steps.length > 1 ? `${i + 1}–${last + 1}` : i + 1}</span>
              <span className="seq-what">
                <span className="seq-name">{n.item && <span className="item-no">{n.item}</span>}{n.text}
                  {g.steps.length > 1 && <span className="seq-qty"> × {g.steps.length}</span>}
                  {s.members.length > 1 && <span className="fine"> (sub-assembly, {s.members.length} parts)</span>}</span>
                <span className="seq-meta">
                  {s.attaches && <>Attaching part of {partName(s.attaches)} · </>}
                  {s.attached_by?.length > 0 && <>After its attaching parts · </>}
                  Out along {s.direction_label}
                  {(s.riders?.length ?? 0) > 0 && ` · carries ${s.riders.length} fitted parts`}
                  {forced && <span className="warn-t"> · forced: no free direction found</span>}
                  {isBroken && <span className="warn-t"> · blocked by a later step</span>}
                </span>
              </span>
              <span className="dir" aria-hidden>{arrow(s.direction_label)}</span>
              {editable && <span className="mv">
                <button className="icon" aria-label="Move earlier" onClick={(e) => { e.stopPropagation(); moveGroup(g.index, -1); }} disabled={g.index === 0}>▲</button>
                <button className="icon" aria-label="Move later" onClick={(e) => { e.stopPropagation(); moveGroup(g.index, 1); }} disabled={g.index === groups.length - 1}>▼</button>
              </span>}
            </li>
          );
        })}
      </ol>
      <p className="fine">Assembly is this sequence in reverse. Parts that stay installed (permanent inserts, press-fit plugs) ride along with their carrier.</p>
    </div>
  );
}

const arrow = (d: string) => ({ "+X": "→", "-X": "←", "+Y": "↗", "-Y": "↙", "+Z": "↑", "-Z": "↓" } as Record<string, string>)[d] ?? "•";
