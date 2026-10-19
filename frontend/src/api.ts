/* Thin client for the studio API. Artifacts are the backend's JSON files, typed loosely on purpose:
   the backend is the single source of truth for their shape (see the backend module docstrings). */
/* eslint-disable @typescript-eslint/no-explicit-any */
export type Json = any;

async function req<T = Json>(method: string, url: string, body?: unknown, form?: FormData): Promise<T> {
  const init: RequestInit = { method };
  if (form) init.body = form;
  else if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers = { "content-type": "application/json" };
  }
  const r = await fetch(url, init);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    let detail: Json = null;
    try { const j = await r.json(); detail = j.detail; msg = typeof j.detail === "string" ? j.detail : j.detail?.message ?? JSON.stringify(j.detail ?? j); } catch { /* not JSON */ }
    throw Object.assign(new Error(msg), { status: r.status, detail });
  }
  return r.status === 204 ? (undefined as T) : r.json();
}

export interface Run {
  id: string; project: string; label: string; created: string; status: "queued" | "running" | "done" | "failed";
  stages: Record<string, { status: string; label: string; seconds?: number; summary?: Json; error?: string }>;
  progress: { stage: string | null; fraction: number; message: string };
  inputs: Record<string, { file: string; sha256: string; bytes: number }>;
  base_run: string | null; signed_off: { at: string; by: string } | null; error: string | null;
  model_file?: string; busy?: boolean; manuf_prefix?: string;
  change?: { kind: "initial" | "revision" | "modification" | "variant"; reference?: string | null; title?: string | null; reason?: string | null };
  publication?: Json;
}
export interface Project {
  id: string; name: string; created: string; settings: Json; runs: string[]; baseline: string | null; run_list?: Run[]; last_run?: Run | null;
  kind?: "product" | "variant"; family?: string; parent?: { project: string; run: string; name: string; label?: string } | null;
}

export const api = {
  system: () => req("GET", "/api/system"),
  projects: () => req<Project[]>("GET", "/api/projects"),
  project: (pid: string) => req<Project>("GET", `/api/projects/${pid}`),
  createProject: (name: string, settings: Json = {}, parent?: string) => req<Project>("POST", "/api/projects", { name, settings, parent }),
  saveSettings: (pid: string, s: Json) => req<Project>("PATCH", `/api/projects/${pid}/settings`, s),
  createRun: (pid: string, form: FormData) => req<Run>("POST", `/api/projects/${pid}/runs`, undefined, form),
  run: (rid: string) => req<Run>("GET", `/api/runs/${rid}`),
  rerun: (rid: string, stage: string) => req("POST", `/api/runs/${rid}/rerun?from_stage=${stage}`),
  artifact: (rid: string, name: string) => req("GET", `/api/runs/${rid}/artifacts/${name}`),
  editRow: (rid: string, row: string, fields: Json) => req("PATCH", `/api/runs/${rid}/ipl/rows/${row}`, { fields }),
  acceptRows: (rid: string, rows: string[] = []) => req("POST", `/api/runs/${rid}/ipl/accept`, { rows }),
  reorder: (rid: string, order: string[]) => req("PUT", `/api/runs/${rid}/sequence`, { order }),
  editStep: (rid: string, kind: string, step: string, text: string) => req("PATCH", `/api/runs/${rid}/procedures/${kind}/steps/${step}`, { text }),
  signoff: (rid: string) => req<Run>("POST", `/api/runs/${rid}/signoff`, { by: "writer" }),
  publish: (rid: string, body: Json) => req("POST", `/api/runs/${rid}/publish`, body),
  publications: (rid: string) => req<Json[]>("GET", `/api/runs/${rid}/publications`),
  // checked documents
  check: (rid: string) => req("GET", `/api/runs/${rid}/check`),
  checkAgain: (rid: string) => req("POST", `/api/runs/${rid}/check`),
  documentUrl: (rid: string, name: string) => `/api/runs/${rid}/documents/${encodeURIComponent(name)}`,
  documentText: async (rid: string, name: string) => {
    const r = await fetch(`/api/runs/${rid}/documents/${encodeURIComponent(name)}`);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.text();
  },
  documentView: (rid: string, name: string, stylesheet?: string) =>
    req<{ html: string; stylesheet: Json }>("GET", `/api/runs/${rid}/documents/${encodeURIComponent(name)}/view${stylesheet ? `?stylesheet=${encodeURIComponent(stylesheet)}` : ""}`),
  documentValidate: (rid: string, name: string, text: string) =>
    req("POST", `/api/runs/${rid}/documents/${encodeURIComponent(name)}/validate`, { text }),
  documentViewText: (rid: string, name: string, text: string, stylesheet?: string) =>
    req<{ html: string; stylesheet: Json }>("POST", `/api/runs/${rid}/documents/${encodeURIComponent(name)}/view`, { text, stylesheet: stylesheet || null }),
  documentSave: (rid: string, name: string, text: string) =>
    req("PUT", `/api/runs/${rid}/documents/${encodeURIComponent(name)}`, { text }),
  documentDiscard: (rid: string, name: string) =>
    req("DELETE", `/api/runs/${rid}/documents/${encodeURIComponent(name)}/edit`),
  // schemas, BREX, rule sets, stylesheets
  conformance: () => req("GET", "/api/conformance"),
  inspectSchemas: (files: File[], paths: string[]) => {
    const f = new FormData();
    files.forEach((x) => f.append("files", x));
    f.append("paths", JSON.stringify(paths));
    return req("POST", "/api/conformance/packages/inspect", undefined, f);
  },
  installSchemas: (staging: string, choice: Json, replace = false) => req("POST", "/api/conformance/packages/install", { staging, choice, replace }),
  installEvaluation: () => req<Json[]>("POST", "/api/conformance/packages/evaluation"),
  setPackage: (key: string, enabled: boolean) => req("PATCH", `/api/conformance/packages/${key}`, { enabled }),
  removePackage: (key: string) => req("DELETE", `/api/conformance/packages/${key}`),
  packageExportUrl: (key: string) => `/api/conformance/package-export/${key}`,
  addBrex: (file: File) => { const f = new FormData(); f.append("file", file); return req("POST", "/api/conformance/brex", undefined, f); },
  removeBrex: (dmc: string, issue: string) => req("DELETE", `/api/conformance/brex/${encodeURIComponent(dmc)}/${encodeURIComponent(issue)}`),
  setSubstitute: (named: string, use: string) => req("PUT", "/api/conformance/brex-substitutes", { named, use }),
  removeSubstitute: (named: string) => req("DELETE", `/api/conformance/brex-substitutes/${encodeURIComponent(named)}`),
  addRules: (file: File, applies_to: Json, name = "") => {
    const f = new FormData(); f.append("file", file); f.append("name", name); f.append("applies_to", JSON.stringify(applies_to));
    return req("POST", "/api/conformance/rules", undefined, f);
  },
  changeRules: (id: string, body: Json) => req("PATCH", `/api/conformance/rules/${id}`, body),
  removeRules: (id: string) => req("DELETE", `/api/conformance/rules/${id}`),
  rulesSourceUrl: (id: string) => `/api/conformance/rules/${id}/source`,
  addStylesheet: (file: File, kind: string, applies_to: Json, order = 100, name = "") => {
    const f = new FormData(); f.append("file", file); f.append("kind", kind); f.append("name", name);
    f.append("applies_to", JSON.stringify(applies_to)); f.append("order", String(order));
    return req("POST", "/api/conformance/stylesheets", undefined, f);
  },
  changeStylesheet: (id: string, body: Json) => req("PATCH", `/api/conformance/stylesheets/${id}`, body),
  removeStylesheet: (id: string) => req("DELETE", `/api/conformance/stylesheets/${id}`),
  stylesheetSourceUrl: (id: string) => `/api/conformance/stylesheets/${id}/source`,
  checkAny: (file: File) => { const f = new FormData(); f.append("file", file); return req("POST", "/api/conformance/check", undefined, f); },
  partsImport: (rid: string) => req("GET", `/api/runs/${rid}/parts-import`),
  confirmPartsImport: (rid: string, table: string, columns: Json) => req("PUT", `/api/runs/${rid}/parts-import`, { table, columns }),
  llmSettings: () => req("GET", "/api/settings/llm"),
  saveLlm: (s: Json) => req("PUT", "/api/settings/llm", s),
  testLlm: (s: Json) => req("POST", "/api/settings/llm/test", s),
  llmModels: (s: Json) => req<{ models: string[] }>("POST", "/api/settings/llm/models", s),
  modelUrl: (rid: string) => `/api/runs/${rid}/model.glb`,
  exportUrl: (rid: string, target: string) => `/api/runs/${rid}/export/${target}`,
};
