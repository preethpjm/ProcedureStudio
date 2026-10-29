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

  // identical parts on the same level are one entry with a quantity: the same item, attaching the same item (or
  // removed from the same item), with the same fitted parts, one part each (sub-assemblies are never merged).
  // Washers go on the line of the screws they sit under. The 3D keeps one step per part (each moves on its own).
  const itemOf = (unit: string | undefined): string => {
    if (!unit) return "";
    const st = (seq?.disassembly ?? []).find((x: Json) => x.unit === unit);
    return (st ? name(st).item || name(st).text : rowByOcc.get(unit)?.item) ?? unit;
  };
  const keyOf = (s: Json) => [name(s).item || name(s).text, s.role, s.members.length > 1 ? s.unit : "",
    s.attaches ? "att:" + itemOf(s.attaches) : "on:" + itemOf(s.on),
    (s.riders ?? []).map((r: string) => rowByOcc.get(r)?.item ?? r.replace(/_\d+$/, "")).sort().join(",")].join("|");
  const groups = useMemo(() => {
    type G = { index: number; start: number; steps: Json[]; main: Json[]; with: Json[][] };
    const out: G[] = [];
    let at = 0;
    steps.forEach((s) => {
      const g = out[out.length - 1];
      const tail = g ? (g.with.length ? g.with[g.with.length - 1] : g.main) : null;
      if (g && tail && keyOf(tail[0]) === keyOf(s)) {
        tail.push(s); g.steps.push(s);
      } else if (g && s.role === "washer" && s.attaches && ["fastener", "nut"].includes(g.main[0].role)
                 && itemOf(g.main[0].attaches) === itemOf(s.attaches)) {
        g.with.push([s]); g.steps.push(s);               // the washers under these screws
      } else {
        out.push({ index: out.length, start: at, steps: [s], main: [s], with: [] });
      }
      at += 1;
    });
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [steps, rowByOcc]);

  // viewer: the slider moves one entry at a time
  useEffect(() => {
    if (!seq) return;
    const hidden = new Set<string>();
    groups.slice(0, cur).forEach((g) => g.steps.forEach((s) => [...s.members, ...(s.riders ?? [])].forEach((m: string) => hidden.add(m))));
    const active = focus ? groups.find((g) => g.steps.some((s) => s.unit === focus)) : groups[cur] ?? null;
    const sel = new Set<string>(active ? active.steps.flatMap((s) => [...s.members, ...(s.riders ?? [])]) : []);
    const ex = new Map<string, [number, number, number]>();
    steps.forEach((s) => [...s.members, ...(s.riders ?? [])].forEach((m: string) => ex.set(m, s.direction)));
    setScene({ selected: sel, hidden, explode: ex, explodeAmount: explode, tint: undefined, dim: null });
  }, [seq, steps, groups, cur, explode, focus, setScene]);
  useEffect(() => () => setScene({ hidden: undefined, explode: undefined, explodeAmount: 0 }), [setScene]);

  useEffect(() => {
    pickHandler.current = (id) => {
      const s = id ? steps.find((x) => x.members.includes(id) || x.riders?.includes(id)) : null;
      setFocus(s?.unit ?? null);
      if (s) document.getElementById(`seq-${s.unit}`)?.scrollIntoView({ block: "nearest" });
    };
    return () => { pickHandler.current = null; };
  }, [steps, pickHandler]);

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
        <p className="lede-s">{groups.length} removal steps ({steps.length} parts) worked out from contacts and blocking directions in the 3D model, part by part: each part comes off right after its attaching parts, and identical parts are one step.
          {seq.conflicts?.length ? ` ${seq.conflicts.length} parts could not be freed and were forced.` : " Every part comes free."}
          {seq.writer_order ? " The order was changed by a writer." : ""}</p>
        <div className="scrub">
          <label htmlFor="scrub">After step</label>
          <input id="scrub" type="range" min={0} max={groups.length} value={cur} onChange={(e) => { setCur(+e.target.value); setFocus(null); }} />
          <output>{cur === 0 ? "Assembled" : `${cur} of ${groups.length}`}</output>
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
          const s = g.steps[0];
          const n = name(s);
          const units = g.steps.map((x) => x.unit);
          const isCur = cur === g.index && !focus, isFocus = !!focus && units.includes(focus), done = g.index < cur;
          const isBroken = units.some((u) => broken.has(u)), forced = g.steps.some((x) => x.forced);
          const dirs = [...new Set(g.steps.map((x) => x.direction_label as string))];
          const held = [...new Set(g.main.map((x) => x.attaches).filter(Boolean))] as string[];
          const ons = [...new Set(g.main.map((x) => x.on).filter(Boolean))] as string[];
          const riders = g.main.reduce((k, x) => k + (x.riders?.length ?? 0), 0);
          return (
            <li key={s.unit} id={`seq-${s.unit}`} className={[isCur && "cur", isFocus && "focus", done && "done", isBroken && "broken", forced && "forced",
              s.attaches && "att", s.attached_by?.length && "held"].filter(Boolean).join(" ")}
              onClick={() => { setFocus(isFocus ? null : s.unit); }}>
              <span className="seq-no">{g.index + 1}</span>
              <span className="seq-what">
                <span className="seq-name">{n.item && <span className="item-no">{n.item}</span>}{n.text}
                  {g.main.length > 1 && <span className="seq-qty"> × {g.main.length}</span>}
                  {g.with.map((w) => { const wn = name(w[0]); return <span key={w[0].unit} className="seq-with"> and {wn.text.toLowerCase()}{wn.item && ` (${wn.item})`}{w.length > 1 && <span className="seq-qty"> × {w.length}</span>}</span>; })}
                  {s.members.length > 1 && <span className="fine"> (sub-assembly, {s.members.length} parts)</span>}</span>
                <span className="seq-meta">
                  {held.length > 0 && <>Attaching parts of {partName(held[0])}{held.length > 1 ? ` × ${held.length}` : ""} · </>}
                  {s.attached_by?.length > 0 && <>After {g.main.length > 1 ? "their" : "its"} attaching parts · </>}
                  {!held.length && ons.length > 0 && <>Off the {partName(ons[0])} · </>}
                  Out along {dirs.join(", ")}{dirs.length === 1 && DIR_FROM[s.direction_from] ? ` (${DIR_FROM[s.direction_from]})` : ""}
                  {s.check && !forced && <span className="warn-t"> · {s.check}</span>}
                  {riders > 0 && ` · carries ${riders} fitted parts`}
                  {forced && <span className="warn-t"> · forced: no free direction found</span>}
                  {isBroken && <span className="warn-t"> · blocked by a later step</span>}
                </span>
              </span>
              <span className="dir" aria-hidden>{dirs.map(arrow).join(" ")}</span>
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

const DIR_FROM: Record<string, string> = { head: "head first", nut: "towards the bolt end", axis: "along its axis", model: "lifted off" };

const arrow = (d: string) => ({ "+X": "→", "-X": "←", "+Y": "↗", "-Y": "↙", "+Z": "↑", "-Z": "↓" } as Record<string, string>)[d] ?? "•";
