import { useEffect, useRef, useState, type ClipboardEvent, type FormEvent, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { useApp } from "../context";
import type { TurnOptions, DocumentUpload, BuildBudget } from "../api";
import ModelPicker from "./ModelPicker";
import { rememberModel, tokenLabel } from "../modelChoices";
import DocumentIcon from "./DocumentIcon";
import BudgetDialog from './BudgetDialog';
import { budgetChoice, money, needsBudget } from '../budget';

const DOCUMENT_TYPES = ".pdf,.txt,.md,.csv,.json,.yaml,.yml,.xml,.html,.rtf,.docx,.xlsx,.pptx,.mpd,.ldr,.dat";

export default function Composer({ llmId, onLlmChange, onSend, onStop, running, autoFocus, initialText = "", initialOptions, budget = budgetChoice(null, true), onBudgetChange }: {
  llmId: string | null;
  onLlmChange: (id: string) => void;
  onSend: (text: string, options: TurnOptions, images: string[], documents: DocumentUpload[], budget: BuildBudget) => Promise<void> | void;
  budget?: BuildBudget;
  onBudgetChange?: (budget: BuildBudget) => Promise<void> | void;
  onStop?: () => void;
  running: boolean;
  autoFocus?: boolean;
  initialText?: string;
  initialOptions?: TurnOptions;
}) {
  const { llms } = useApp();
  const [text, setText] = useState(initialText);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLTextAreaElement>(null);
  const upload = useRef<HTMLInputElement>(null);
  const documentUpload = useRef<HTMLInputElement>(null);
  const [selection, setSelection] = useState<{ modelId: string | null; options: TurnOptions }>({ modelId: llmId,
    options: { ...initialOptions, mode: initialOptions && initialOptions.mode !== "agent" ? "plan" : "agent", permissions: initialOptions?.permissions ?? "full" } });
  const [images, setImages] = useState<{ name: string; url: string; size: number }[]>([]);
  const [documents, setDocuments] = useState<(DocumentUpload & { size: number })[]>([]);
  const [error, setError] = useState("");
  const [budgetDialog, setBudgetDialog] = useState<'start' | 'edit' | null>(null);
  const resolveBudget = useRef<((budget: BuildBudget | null) => void) | null>(null);
  const submitLock = useRef(false);
  useEffect(() => () => { resolveBudget.current?.(null); }, []);
  const model = llms.find(m => m.id === llmId);
  const profile = model?.profile;
  const disabled = running || busy || budgetDialog !== null;
  // Resolve synchronously so a quick model switch + Send cannot submit the
  // previous provider's effort/context while waiting for an effect to run.
  const sameModel = selection.modelId === llmId;
  const saved = selection.options;
  const options: TurnOptions = { ...saved,
    effort: sameModel && saved.effort && profile?.efforts.includes(saved.effort) ? saved.effort : profile?.default_effort ?? null,
    context_tokens: sameModel && saved.context_tokens && profile?.context_budgets.includes(saved.context_tokens) ? saved.context_tokens : profile?.context_window ?? null,
  };
  const setOptions = (next: TurnOptions) => setSelection({ modelId: llmId, options: next });
  function chooseModel(id: string) {
    const next = llms.find(m => m.id === id)?.profile;
    setSelection({ modelId: id, options: { ...options, effort: next?.default_effort ?? null, context_tokens: next?.context_window ?? null } });
    onLlmChange(id);
  }
  useEffect(() => setText(initialText), [initialText]);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 240) + "px";
  }, [text]);

  async function submit(e?: FormEvent) {
    e?.preventDefault();
    const value = text.trim();
    if (!value || disabled || !model || submitLock.current) return;
    submitLock.current = true; setBusy(true);
    try {
      setError("");
      let choice: BuildBudget | null = budget;
      if (needsBudget(budget, options)) {
        choice = await new Promise<BuildBudget | null>(resolve => { resolveBudget.current = resolve; setBudgetDialog('start'); });
      }
      if (choice === null) return;
      await onSend(value, options, images.map(i => i.url), documents.map(({ name, data }) => ({ name, data })), choice);
      rememberModel(model.id);
      setText(""); setImages([]); setDocuments([]);
    } catch (e) { setError((e as Error).message); }
    finally { submitLock.current = false; setBusy(false); }
  }
  function closeBudget() {
    resolveBudget.current?.(null); resolveBudget.current = null; setBudgetDialog(null);
  }
  async function chooseBudget(choice: BuildBudget) {
    if (budgetDialog === 'start') { resolveBudget.current?.(choice); resolveBudget.current = null; }
    else await onBudgetChange?.(choice);
    setBudgetDialog(null);
  }
  async function removeBudget() {
    setBusy(true); setError('');
    try { await onBudgetChange?.(budgetChoice(null)); }
    catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  }
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); }
  }
  function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(e.clipboardData.files).filter(file => file.type.startsWith("image/"));
    if (!files.length) return;
    // A copied image can also contain its URL or HTML; attach only the image.
    e.preventDefault();
    if (!disabled && model) void attach(files);
  }
  async function attach(files: File[], kind: "image" | "document" = "image") {
    if (!files.length) return;
    setError("");
    if (files.length + (kind === "image" ? images.length : documents.length) > 4 || files.some(f => f.size > 5 * 1024 * 1024)) {
      setError(`Attach up to 4 ${kind === "image" ? "images" : "documents"}, at most 5 MB each.`); return;
    }
    if ([...images, ...documents, ...files].reduce((total, f) => total + f.size, 0) > 12 * 1024 * 1024) {
      setError("Images and documents must total at most 12 MB."); return;
    }
    if (kind === "image" && files.some(f => !["image/png", "image/jpeg", "image/webp"].includes(f.type))) {
      setError("Choose PNG, JPEG or WebP images."); return;
    }
    if (kind === "document" && files.some(f => !DOCUMENT_TYPES.split(",").includes("." + f.name.split(".").pop()?.toLowerCase()))) {
      setError("Choose a PDF, text, Office or LDraw document. Use the image button for images."); return;
    }
    setBusy(true);
    try {
      const added = await Promise.all(files.map(file => new Promise<{ name: string; url: string; size: number }>((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve({ name: file.name, url: String(reader.result), size: file.size });
        reader.onerror = () => reject(new Error("Cannot read attachment")); reader.readAsDataURL(file);
      })));
      if (kind === "image") setImages(current => [...current, ...added]);
      else setDocuments(current => [...current, ...added.map(({ name, url, size }) => ({ name, data: url, size }))]);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  const effortNames: Record<string, string> = { none: "No reasoning", low: "Low", medium: "Medium", high: "High", xhigh: "XHigh", max: "Max" };

  return <form className="composer" onSubmit={submit}>
    {budgetDialog && <BudgetDialog initial={budget} starting={budgetDialog === 'start'} onChoose={chooseBudget}
      onCancel={closeBudget} returnFocus={() => ref.current?.focus()} />}
    {!llms.length && <div className="composer-notice">No model configured yet. <Link to="/settings">Add a model in Settings</Link> to start building.</div>}
    <div className="composer-input">
      <button type="button" className="attach-button" aria-label="Attach images" title="Attach images (up to 4)" disabled={disabled || !model} onClick={() => upload.current?.click()}>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
          <rect x="3" y="3" width="18" height="18" rx="3" /><circle cx="8" cy="8" r="1.5" /><path d="m3 16 5-5 4 4 4-5 5 6" />
        </svg>
      </button>
      <button type="button" className="attach-button" aria-label="Attach documents" title="Attach documents (up to 4)" disabled={disabled || !model} onClick={() => documentUpload.current?.click()}>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
          <path d="m8 13 7-7a3 3 0 0 1 4 4L9 20a5 5 0 0 1-7-7L13 2a2 2 0 0 1 3 3L5 16" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      <input ref={documentUpload} hidden type="file" aria-label="Document files" accept={DOCUMENT_TYPES} multiple disabled={disabled} onChange={e => {
        const files = Array.from(e.target.files ?? []); e.target.value = ""; void attach(files, "document");
      }} />
      <input ref={upload} hidden type="file" aria-label="Image files" accept="image/png,image/jpeg,image/webp" multiple disabled={disabled} onChange={e => {
        const files = Array.from(e.target.files ?? []); e.target.value = ""; void attach(files);
      }} />
      <textarea ref={ref} rows={2} value={text} autoFocus={autoFocus} aria-label="Message" placeholder="Describe a model to build…"
        onChange={e => setText(e.target.value)} onKeyDown={onKeyDown} onPaste={onPaste} />
    </div>
    {!!images.length && <div className="attachments">{images.map((img, index) => <div key={index}>
      <img src={img.url} alt={img.name} /><button type="button" aria-label={`Remove ${img.name}`} disabled={disabled} onClick={() => setImages(images.filter((_, i) => i !== index))}>✕</button>
    </div>)}</div>}
    {!!documents.length && <div className="document-attachments">{documents.map((doc, index) => <div className="document-chip" key={index}>
      <DocumentIcon /><span>{doc.name}</span><button type="button" className="icon-button" aria-label={`Remove ${doc.name}`} disabled={disabled} onClick={() => setDocuments(documents.filter((_, i) => i !== index))}>✕</button>
    </div>)}</div>}
    {error && <div className="banner error" role="alert">{error}</div>}
    <div className="composer-budget">
      <span>{budget.status === 'set' ? `Parts budget ${money(budget.amount_cents!)} · up to ${money(budget.maximum_cents!)}` :
        budget.status === 'pending' ? 'Choose a parts budget before building' : 'No parts budget'}</span>
      {onBudgetChange && <><button type="button" disabled={disabled} onClick={() => setBudgetDialog('edit')}>
        {budget.status === 'set' ? 'Change budget' : 'Set budget'}</button>
        {budget.status === 'set' && <button type="button" disabled={disabled} onClick={removeBudget}>Remove budget</button>}</>}
    </div>
    <div className="composer-toolbar">
      <div className="turn-options">
        <ModelPicker value={llmId} onChange={chooseModel} disabled={disabled || !llms.length} />
        <select className="compact-control" aria-label="Mode" title="Build with Agent, or work out a design in Plan" disabled={disabled} value={options.mode}
          onChange={e => setOptions({ ...options, mode: e.target.value as TurnOptions["mode"] })}>
          <option value="agent">Agent</option><option value="plan">Plan</option>
        </select>
        {options.mode === "agent" && <select className="compact-control" aria-label="Permissions" title="Permissions for tools in the container" disabled={disabled} value={options.permissions}
          onChange={e => setOptions({ ...options, permissions: e.target.value as TurnOptions["permissions"] })}>
          <option value="ask">Ask first</option><option value="full">Full access</option><option value="read_only">Read only</option>
        </select>}
        {!!profile?.efforts.length && <select className="compact-control" aria-label="Effort" title="Reasoning effort" disabled={disabled} value={options.effort ?? profile.default_effort ?? ""}
          onChange={e => setOptions({ ...options, effort: e.target.value })}>
          {profile.efforts.map(e => <option value={e} key={e}>{effortNames[e] ?? e}</option>)}
        </select>}
        {!!profile?.context_budgets.length && <select className="compact-control" aria-label="Context budget" title="Model context window (tokens)" disabled={disabled} value={options.context_tokens ?? profile.context_window ?? ""}
          onChange={e => setOptions({ ...options, context_tokens: Number(e.target.value) })}>
          {profile.context_budgets.map(n => <option value={n} key={n}>{tokenLabel(n)}</option>)}
        </select>}
      </div>
      {running ? <button type="button" className="danger send-button" onClick={onStop}>Stop</button> :
        <button type="submit" className="primary send-button" disabled={!text.trim() || busy || !model}>Send <span aria-hidden>↑</span></button>}
    </div>
  </form>;
}
