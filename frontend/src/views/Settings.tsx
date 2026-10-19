import { useEffect, useState } from "react";
import { api, Json } from "../api";
import { useStudio } from "../state";

/* The language model connection. Optional: procedures are complete without it. When connected, it only
   rewords template steps, and every reworded step must pass the STE checker and the fact check (items,
   quantities, torques) or the template text is kept. */

const PROVIDERS: { id: string; label: string; note: string }[] = [
  { id: "none", label: "None", note: "Steps are written from templates only. Deterministic and offline." },
  { id: "ollama", label: "Ollama (offline)", note: "A model on this computer or your network, for example Qwen 2.5 / Qwen 3. Nothing leaves your network." },
  { id: "openai", label: "OpenAI-compatible server", note: "vLLM, LM Studio, LocalAI or an internal gateway that speaks the OpenAI chat API." },
  { id: "bedrock", label: "Amazon Bedrock", note: "Claude or another Bedrock model in your AWS account, optionally through a VPC interface endpoint (PrivateLink)." },
  { id: "anthropic", label: "Anthropic API", note: "Claude through the Anthropic API." },
];
const DEFAULTS: Record<string, Json> = {
  ollama: { base_url: "http://127.0.0.1:11434/v1", model: "qwen2.5:7b", batch_size: 8, timeout: 300 },
  openai: { base_url: "http://127.0.0.1:8000/v1", model: "", batch_size: 15, json_mode: true },
  bedrock: { region: "eu-west-3", model: "eu.anthropic.claude-sonnet-4-5-20250929-v1:0", endpoint_url: "", profile: "" },
  anthropic: { model: "claude-sonnet-4-5", api_key_env: "ANTHROPIC_API_KEY" },
};

export function SettingsView() {
  const { toast, refreshSystem } = useStudio();
  const [s, setS] = useState<Json | null>(null);
  const [source, setSource] = useState("");
  const [status, setStatus] = useState<Json>(null);
  const [models, setModels] = useState<string[]>([]);
  const [test, setTest] = useState<Json>(null);
  const [busy, setBusy] = useState("");

  useEffect(() => {
    api.llmSettings().then((r) => { setS(r.settings); setSource(r.source); setStatus(r.status); }).catch((e) => toast(e.message, "bad"));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  if (!s) return <div className="view"><p className="fine">Loading…</p></div>;
  const p = s.provider || "none";
  const set = (k: string, v: unknown) => setS({ ...s, [k]: v });
  const choose = (id: string) => { setS({ provider: id, ...(DEFAULTS[id] ?? {}) }); setModels([]); setTest(null); };
  const field = (k: string, label: string, ph = "", type = "text") => (
    <label key={k}><span>{label}</span><input type={type} value={s[k] ?? ""} placeholder={ph} onChange={(e) => set(k, type === "number" ? +e.target.value : e.target.value)} /></label>);

  const save = async () => {
    setBusy("save");
    try { const r = await api.saveLlm(s); setStatus(r.status); setSource("app"); refreshSystem(); toast("Language model saved. New runs use it; to reword an existing run, use Procedures → Reword."); }
    catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(""); }
  };
  const runTest = async () => {
    setBusy("test"); setTest(null);
    try { setTest(await api.testLlm(s)); } catch (e) { setTest({ ok: false, message: (e as Error).message }); } finally { setBusy(""); }
  };
  const listModels = async () => {
    setBusy("models");
    try { const r = await api.llmModels(s); setModels(r.models); if (!r.models.length) toast("The server lists no models. Pull one first: ollama pull qwen2.5:7b", "bad"); }
    catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(""); }
  };

  return (
    <div className="view settings">
      <header className="view-head">
        <h1>Settings</h1>
        <p className="lede-s">Language model for rewording procedure steps. It is optional: Studio writes complete STE procedures from templates. A connected model only improves the wording, and each reworded step goes through the STE checker and the fact check; a step that changes an item, quantity or torque keeps the template text.</p>
      </header>
      <section className="panel-sec">
        <h2>Language model</h2>
        <p className="fine">Now: {status?.available ? <span className="ok-t">{status.provider} connected{status.model ? ` · ${status.model}` : ""}</span> : status?.provider === "none" ? "templates only" : <span className="warn-t">{status?.provider} not reachable</span>}{source === "studio.yaml" ? " (from studio.yaml)" : ""}.</p>
        <div className="provider-list" role="radiogroup" aria-label="Provider">
          {PROVIDERS.map((x) => (
            <label key={x.id} className={`check ${p === x.id ? "on" : ""}`}>
              <input type="radio" name="provider" checked={p === x.id} onChange={() => choose(x.id)} />
              <span><b>{x.label}</b><br /><span className="fine">{x.note}</span></span>
            </label>))}
        </div>
        {p !== "none" && <div className="fields llm-fields">
          {(p === "ollama" || p === "openai") && <>
            {field("base_url", "Server address", DEFAULTS[p].base_url)}
            <label><span>Model</span>
              <span className="pair">
                {models.length ? <select value={s.model ?? ""} onChange={(e) => set("model", e.target.value)}>
                  {!models.includes(s.model) && <option value={s.model}>{s.model || "Choose…"}</option>}
                  {models.map((m) => <option key={m}>{m}</option>)}</select> :
                  <input value={s.model ?? ""} placeholder="qwen2.5:7b" onChange={(e) => set("model", e.target.value)} />}
                <button type="button" className="btn" onClick={listModels} disabled={busy === "models"}>{busy === "models" ? "…" : "List models"}</button>
              </span></label>
            {p === "openai" && field("api_key", "API key (if the server needs one)", "")}
            {field("batch_size", "Steps per request", "8", "number")}
            {field("timeout", "Timeout per request, s", "300", "number")}
          </>}
          {p === "bedrock" && <>
            {field("region", "AWS region", "eu-west-3")}
            {field("model", "Model or inference profile id", DEFAULTS.bedrock.model)}
            {field("endpoint_url", "VPC endpoint URL (optional)", "https://vpce-…bedrock-runtime.eu-west-3.vpce.amazonaws.com")}
            {field("profile", "AWS profile (optional)", "default credentials chain")}
            <p className="fine wide">Credentials come from the server's AWS credentials chain (instance role, profile or environment), never from this page.</p>
          </>}
          {p === "anthropic" && <>
            {field("model", "Model", "claude-sonnet-4-5")}
            {field("api_key_env", "Environment variable with the key", "ANTHROPIC_API_KEY")}
          </>}
        </div>}
        <div className="actions">
          {p !== "none" && <button className="btn" onClick={runTest} disabled={!!busy}>{busy === "test" ? "Testing… (a local model can take a minute)" : "Test with two sample steps"}</button>}
          <button className="btn primary" onClick={save} disabled={!!busy}>Save</button>
        </div>
        {test && <div className={`callout ${test.ok ? "info" : "bad"}`}>
          {test.ok ? <div className="test-out">
            <b>Answered in {test.seconds} s.</b>
            <table className="grid compact static"><thead><tr><th>Sent</th><th>Returned</th></tr></thead>
              <tbody>{test.sample.map((x: Json, i: number) => <tr key={x.id}><td>{x.text}</td><td>{test.answer?.[i]?.text ?? <span className="warn-t">missing</span>}</td></tr>)}</tbody></table>
            <span className="fine">During a run, each answer is checked; one that changes a value is discarded.</span>
          </div> : <span>Not working: {test.message}</span>}
        </div>}
      </section>
    </div>
  );
}
