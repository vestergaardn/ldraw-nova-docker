import { useEffect, useRef, useState } from "react";
import { api, type AssemblyGuide } from "../api";

export default function AssemblyGuideButton({ modelUrl }: { modelUrl: string }) {
  const [job, setJob] = useState<AssemblyGuide | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const busy = starting || job?.status === "queued" || job?.status === "rendering";

  useEffect(() => {
    if (!job || !["queued", "rendering"].includes(job.status)) return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const next = await api.assemblyGuide(job.id);
        if (!cancelled) setJob(next);
      } catch (e) {
        if (!cancelled) { setError((e as Error).message); setJob(null); }
      }
    }, 2000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [job]);

  async function generate() {
    setStarting(true); setError("");
    try {
      const result = await api.createAssemblyGuide(modelUrl);
      if (mounted.current) setJob(result);
    } catch (e) {
      if (mounted.current) setError((e as Error).message);
    } finally {
      if (mounted.current) setStarting(false);
    }
  }

  const failure = error || (job?.status === "failed" ? job.error : "");
  return <div className="assembly-guide">
    {job?.status === "ready" && job.download_url ?
      <a className="button assembly-guide-button" href={job.download_url} download>Download assembly PDF</a> :
      <button className="assembly-guide-button" onClick={generate} disabled={busy} aria-busy={busy}>
        {busy && <span className="spinner" aria-hidden="true" />}
        {busy ? (job?.status === "queued" ? "Guide queued..." : "Creating assembly guide...") :
          failure ? "Retry assembly guide" : "Create assembly guide"}
      </button>}
    <span className="muted small" role="status" aria-live="polite">
      {job?.status === "ready" ? `${job.pages ?? ""} pages · Printable PDF booklet` :
        busy ? "Rendering the model's saved building steps. This can take a few minutes." :
        "Numbered steps and a complete parts list"}
    </span>
    {failure && <span className="warn-text small" role="alert">{failure}</span>}
  </div>;
}
