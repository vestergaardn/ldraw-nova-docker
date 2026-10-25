import { useEffect, useRef, useState } from "react";
import { api, type AssemblyGuide, type GuideOptions } from "../api";

const defaults: GuideOptions = {template:"creator-31134",improve:true,print_format:"native"};
function savedGuide(key: string): { id: string; options: GuideOptions } | null {
  try {
    const saved=JSON.parse(localStorage.getItem(key) || "null");
    if (saved && /^[a-f0-9]{32}$/.test(saved.id) &&
        ["creator-31134","city-clear"].includes(saved.options.template) &&
        ["native","letter"].includes(saved.options.print_format) && typeof saved.options.improve==="boolean") return saved;
  } catch { /* Storage can be disabled; the current job still works. */ }
  return null;
}
function elapsed(seconds: number) {
  return seconds>=60 ? `${Math.floor(seconds/60)}m ${seconds%60}s` : `${seconds}s`;
}

export default function AssemblyGuideButton({ modelUrl, revision }: { modelUrl: string; revision: number }) {
  const storageKey=`lego-guide-v1:${modelUrl}:${revision}`;
  const [saved] = useState(() => savedGuide(storageKey));
  const [job, setJob] = useState<AssemblyGuide | null>(() => saved ? {id:saved.id,status:"queued"} : null);
  const [options, setOptions] = useState<GuideOptions>(saved?.options || defaults);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const busy = starting || job?.status === "queued" || job?.status === "rendering";

  useEffect(() => {
    try {
      if (job) localStorage.setItem(storageKey,JSON.stringify({id:job.id,options}));
      else localStorage.removeItem(storageKey);
    } catch { /* Rendering does not depend on browser storage. */ }
  }, [job?.id,options,storageKey]);

  useEffect(() => {
    if (!job || !["queued", "rendering"].includes(job.status)) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const id=job.id;
    async function poll() {
      try {
        const next = await api.assemblyGuide(id);
        if (cancelled) return;
        setJob(next);setError("");
        if (!["queued","rendering"].includes(next.status)) return;
      } catch (e) {
        if (cancelled) return;
        if ((e as Error).message.includes("Unknown assembly guide")) {
          setJob(null);setError("This saved guide is unavailable. Please create it again.");return;
        }
        setError("Connection interrupted. Checking again; your guide keeps running on the server.");
      }
      timer=setTimeout(poll,2000);
    }
    timer=setTimeout(poll,0);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [job?.id,job?.status]);

  async function generate() {
    setStarting(true); setError("");
    try {
      const result = await api.createAssemblyGuide(modelUrl, options);
      if (mounted.current) setJob(result);
    } catch (e) {
      if (mounted.current) setError((e as Error).message);
    } finally {
      if (mounted.current) setStarting(false);
    }
  }

  const failure = error || (job?.status === "failed" ? job.error : "");
  const progress=job?.progress;
  const labels={checking:"Checking model geometry",steps:"Rendering instruction images",verify:"Checking the complete parts list",parts:"Rendering parts images",layout:"Writing the PDF booklet",complete:"Finishing the download"};
  const detail=job?.status==="queued" ? "Waiting for the renderer" : progress ?
    `${labels[progress.stage]}${progress.total ? ` · ${progress.completed} of ${progress.total}` : ""}` : "Preparing the assembly guide";
  return <div className="assembly-guide">
    <details className="guide-options"><summary>Guide options</summary>
      <label>Page style<select value={options.template} disabled={busy} onChange={e => { setOptions({...options,template:e.target.value as GuideOptions['template']}); setJob(null); }}>
        <option value="creator-31134">Creator blue</option><option value="city-clear">City clear</option>
      </select></label>
      <label>Print format<select value={options.print_format} disabled={busy} onChange={e => { setOptions({...options,print_format:e.target.value as GuideOptions['print_format']}); setJob(null); }}>
        <option value="native">Native booklet pages</option><option value="letter">US Letter · folded booklet</option>
      </select></label>
      <label className="guide-improve"><input type="checkbox" checked={options.improve} disabled={busy} onChange={e => { setOptions({...options,improve:e.target.checked}); setJob(null); }} /> Split crowded steps and improve views</label>
      <p className="muted small">Model parts and positions stay unchanged. Print Letter booklets at actual size, two-sided, short-edge flip.</p>
    </details>
    {job?.status === "ready" && job.download_url ?
      <a className="button assembly-guide-button" href={job.download_url} download>Download assembly PDF</a> :
      <button className="assembly-guide-button" onClick={generate} disabled={busy} aria-busy={busy}>
        {busy && <span className="spinner" aria-hidden="true" />}
        {busy ? (job?.status === "queued" ? "Guide queued..." : "Creating assembly guide...") :
          failure ? "Retry assembly guide" : "Create assembly guide"}
      </button>}
    {busy && progress?.total ? <progress className="guide-progress" value={progress.completed} max={progress.total} aria-label={labels[progress.stage]} /> : null}
    <span className="muted small guide-status" role="status" aria-live="polite">
      {job?.status === "ready" ? `${job.pages ?? ""} pages · Printable PDF booklet` :
        busy ? `${detail}${job?.elapsed_seconds != null ? ` · ${elapsed(job.elapsed_seconds)} elapsed` : ""}. You can leave this page and return later.` :
        "Numbered steps and a complete parts list"}
    </span>
    {failure && <span className="warn-text small" role="alert">{failure}</span>}
  </div>;
}
