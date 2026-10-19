import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, Json, Project, Run } from "./api";

/* Shared studio state: the selected project and run, the scene shown in the 3D viewer, and artifact loading.
   Views describe what the viewer should show (selection, tint, ghosting, explode, hidden) and the shell draws it. */

export interface Scene {
  selected: Set<string>;
  tint?: Map<string, string>;
  dim?: Set<string> | null;
  explode?: Map<string, [number, number, number]>;
  explodeAmount?: number;
  hidden?: Set<string>;
}

export type ViewId = "inputs" | "match" | "ipl" | "sequence" | "procedures" | "check" | "publish" | "changes" | "schemas" | "settings";

interface Ctx {
  system: Json | null;
  projects: Project[];
  project: Project | null;
  run: Run | null;
  version: number;                    // bumps whenever run outputs may have changed
  view: ViewId; setView: (v: ViewId) => void;
  procKind: string; setProcKind: (k: string) => void;
  selectProject: (pid: string | null) => void;
  selectRun: (rid: string | null) => void;
  refreshProjects: () => Promise<void>;
  refreshRun: () => Promise<void>;
  refreshSystem: () => void;
  touch: () => void;                  // local edit happened: reload artifacts
  scene: Scene; setScene: (s: Partial<Scene>) => void;
  nodes: string[]; setNodes: (n: string[]) => void;
  pickHandler: React.MutableRefObject<((id: string | null, add: boolean) => void) | null>;
  toast: (msg: string, kind?: "ok" | "bad") => void;
  editable: boolean;
}

const C = createContext<Ctx | null>(null);
export const useStudio = () => useContext(C)!;

export function StudioProvider({ children, toast }: { children: React.ReactNode; toast: Ctx["toast"] }) {
  const [system, setSystem] = useState<Json | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [version, setVersion] = useState(0);
  const [view, setView] = useState<ViewId>("inputs");
  const [procKind, setProcKind] = useState("disassembly");
  const [scene, setSceneState] = useState<Scene>({ selected: new Set() });
  const [nodes, setNodes] = useState<string[]>([]);
  const pickHandler = useRef<((id: string | null, add: boolean) => void) | null>(null);
  const fingerprint = useRef("");

  const params = new URLSearchParams(location.search);
  const initial = useRef({ project: params.get("project"), run: params.get("run"), view: params.get("view") as ViewId | null });

  const refreshProjects = useCallback(async () => {
    const ps = await api.projects();
    setProjects(ps);
  }, []);

  useEffect(() => {
    api.system().then(setSystem).catch((e) => toast(`The studio server did not answer: ${e.message}`, "bad"));
    refreshProjects().then(() => {
      const { project: p, view: v } = initial.current;
      if (v) setView(v);
      if (p) selectProject(p);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // first project by default
  useEffect(() => {
    if (!project && projects.length && !initial.current.project) selectProject(projects[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projects]);

  const selectProject = useCallback(async (pid: string | null) => {
    if (!pid) { setProject(null); setRun(null); return; }
    try {
      const p = await api.project(pid);
      setProject(p);
      const want = initial.current.run && p.runs.includes(initial.current.run) ? initial.current.run : p.runs[p.runs.length - 1];
      initial.current.run = null;
      if (want) selectRun(want); else setRun(null);
    } catch (e) { toast((e as Error).message, "bad"); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const selectRun = useCallback(async (rid: string | null) => {
    if (!rid) { setRun(null); return; }
    const r = await api.run(rid);
    fingerprint.current = "";
    setRun(r);
    setSceneState({ selected: new Set() });
    setVersion((v) => v + 1);
  }, []);

  const refreshRun = useCallback(async () => {
    if (!run) return;
    const r = await api.run(run.id);
    const fp = JSON.stringify([r.status, Object.values(r.stages || {}).map((s) => [s.status, s.seconds])]);
    if (fp !== fingerprint.current) { fingerprint.current = fp; setVersion((v) => v + 1); }
    setRun(r);
  }, [run]);

  // keep the URL shareable
  useEffect(() => {
    const q = new URLSearchParams();
    if (project) q.set("project", project.id);
    if (run) q.set("run", run.id);
    q.set("view", view);
    history.replaceState(null, "", `?${q}`);
  }, [project, run, view]);

  // poll while the pipeline runs
  const busy = !!run && (run.busy || run.status === "running" || run.status === "queued");
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(() => { refreshRun().catch(() => undefined); }, 900);
    return () => clearInterval(t);
  }, [busy, refreshRun]);

  const refreshSystem = useCallback(() => { api.system().then(setSystem).catch(() => undefined); }, []);
  const setScene = useCallback((s: Partial<Scene>) => setSceneState((o) => ({ ...o, ...s })), []);
  const touch = useCallback(() => { setVersion((v) => v + 1); setTimeout(() => refreshRun().catch(() => undefined), 400); }, [refreshRun]);

  return (
    <C.Provider value={{ system, projects, project, run, version, view, setView, procKind, setProcKind, selectProject, selectRun, refreshProjects,
      refreshRun, refreshSystem, touch, scene, setScene, nodes, setNodes, pickHandler, toast, editable: !!run && !run.signed_off && !busy }}>
      {children}
    </C.Provider>
  );
}

/** Load a run artifact (JSON) and reload it whenever the run's outputs change. */
export function useArtifact<T = Json>(name: string, enabled = true): { data: T | null; error: string | null; reload: () => void } {
  const { run, version } = useStudio();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [n, setN] = useState(0);
  const rid = run?.id;
  useEffect(() => {
    if (!rid || !enabled) { setData(null); return; }
    let live = true;
    api.artifact(rid, name).then((d) => { if (live) { setData(d); setError(null); } })
      .catch((e) => { if (live) { setData(null); setError(e.message); } });
    return () => { live = false; };
  }, [rid, name, version, n, enabled]);
  return { data, error, reload: () => setN((x) => x + 1) };
}

/** The occurrence ids in the model for a STEP part number (GLB node names are safe(PN)_n). */
export function occsForPn(nodes: string[], pn: string | null | undefined): string[] {
  if (!pn) return [];
  const safe = pn.trim().replace(/[^A-Za-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
  return nodes.filter((n) => n.slice(0, n.lastIndexOf("_")) === safe);
}

export const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;
