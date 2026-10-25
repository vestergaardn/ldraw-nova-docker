import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { api, type EnvironmentVariable } from "../api";
import { useApp } from "../context";

type Row = { key: string; id?: string; name: string; value: string | null; has_value: boolean; fixed: boolean; editing?: boolean };
type Check = { preconfigured: boolean; saved: boolean };
const toRows = (variables: EnvironmentVariable[]): Row[] => variables.map(v => ({ ...v, key: v.id }));
const HINTS: Record<string, string> = { TYPESAFE_API_KEY: "Required for finding parts with Jev's semantic search." };

function useEnvironmentCheck(row: Row): Check {
  const [check, setCheck] = useState<Check>({ preconfigured: false, saved: false });
  useEffect(() => {
    let active = true;
    setCheck({ preconfigured: false, saved: false });
    if (!row.name.trim()) return;
    const timer = setTimeout(() => {
      api.checkEnvironment(row.name.trim(), row.id).then(result => { if (active) setCheck(result); }).catch(() => {});
    }, 250);
    return () => { active = false; clearTimeout(timer); };
  }, [row.name, row.id]);
  return check;
}

function ReplacementWarning({ check }: { check: Check }) {
  const warning = check.preconfigured
    ? "This name is already preconfigured. The value you save here takes precedence and replaces that value for new requests."
    : check.saved ? "This variable already has a saved row. Edit that row instead of adding it again." : "";
  return warning ? <span className="environment-warning" tabIndex={0} role="img" aria-label={warning} title={warning}>
    <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M10 2 19 18H1Z" strokeLinejoin="round" /><path d="M10 7v5m0 2v1" strokeLinecap="round" /></svg>
    <span className="environment-tooltip" aria-hidden="true">{warning}</span>
  </span> : null;
}

const Icon = ({ children }: { children: ReactNode }) =>
  <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;
const LockIcon = () => <Icon><rect x="3" y="7" width="10" height="7" rx="1.5" /><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" /></Icon>;
const KeyIcon = () => <Icon><circle cx="5.5" cy="10.5" r="2.5" /><path d="m7.3 8.7 5.2-5.2M10.5 5.5l1.5 1.5" /></Icon>;
const PlusIcon = () => <Icon><path d="M8 3v10M3 8h10" /></Icon>;
const TrashIcon = () => <Icon><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" /></Icon>;

function EnvironmentItem({ row, index, busy, onChange, onEdit, onCancel, onRemove }: {
  row: Row; index: number; busy: boolean;
  onChange: (patch: Partial<Row>) => void; onEdit: () => void; onCancel: () => void; onRemove: () => void;
}) {
  const check = useEnvironmentCheck(row);
  const isNew = !row.id;
  const missing = row.fixed && !row.has_value;
  const open = isNew || missing || row.editing;
  const hint = HINTS[row.name];
  const label = row.name || `variable ${index + 1}`;

  return <li className={`env-item${open ? " is-open" : ""}`}>
    <div className="env-summary">
      <span className={`env-icon${missing ? " is-missing" : ""}`}>{row.has_value ? <LockIcon /> : <KeyIcon />}</span>
      <div className="env-title">
        <div className="env-name-line">
          <code className="env-name">{row.name || "New variable"}</code>
          {row.fixed && (missing ? <span className="badge warn">Missing</span> : <span className="badge">Required</span>)}
          {!open && check.preconfigured && <span className="badge" title="This value replaces the preconfigured value for new requests.">Overrides preset</span>}
        </div>
        {hint && <p id={`env-hint-${row.key}`} className="env-meta">{hint}</p>}
        {!hint && !open && <p className="env-meta">Saved · value hidden</p>}
      </div>
      <div className="env-actions">
        {!open && <>
          <span className="env-mask" aria-hidden="true">••••••••••••</span>
          <button type="button" className="env-button" aria-label={`Edit ${label}`} disabled={busy} onClick={onEdit}>Edit</button>
        </>}
        {!row.fixed && <button type="button" className="env-icon-button" aria-label={`Remove ${label}`} title="Remove" disabled={busy} onClick={onRemove}><TrashIcon /></button>}
      </div>
    </div>
    {open && <div className={`env-editor${row.fixed ? " is-fixed" : ""}`}>
      {!row.fixed && <label className="env-field">
        <span>Name</span>
        <input aria-label={`Variable name ${index + 1}`} required pattern="[A-Za-z_][A-Za-z0-9_]*" maxLength={255}
          autoComplete="off" spellCheck={false} placeholder="MY_API_KEY" disabled={busy} autoFocus={isNew}
          value={row.name} onChange={e => onChange({ name: e.target.value })} />
      </label>}
      <label className="env-field">
        <span>{row.has_value ? "New value" : "Value"}</span>
        <span className="environment-value">
          <input aria-label={`${label} value`} aria-describedby={hint ? `env-hint-${row.key}` : undefined} type="password" autoComplete="new-password" spellCheck={false} maxLength={65536}
            placeholder={row.has_value ? "Enter a new value to replace the saved one" : "Paste the value"} disabled={busy} autoFocus={row.editing && row.fixed}
            value={row.value ?? ""} onChange={e => onChange({ value: e.target.value })} />
          <ReplacementWarning check={check} />
        </span>
      </label>
      {row.editing && <button type="button" className="env-button env-cancel" disabled={busy} onClick={onCancel}>Cancel</button>}
    </div>}
  </li>;
}

export default function EnvironmentSettings() {
  const { refreshLlms } = useApp();
  const [rows, setRows] = useState<Row[]>([]);
  const [saved, setSaved] = useState<Row[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const nextKey = useRef(0);

  function load(variables: EnvironmentVariable[]) {
    const next = toRows(variables);
    setRows(next); setSaved(next);
  }

  useEffect(() => {
    let active = true;
    api.environment().then(result => {
      if (active) { load(result.variables); setLoaded(true); }
    }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, []);

  // A null value tells the server to keep the saved secret, so only typed values count as changes.
  const dirty = rows.length !== saved.length || rows.some(row => {
    const original = saved.find(s => s.key === row.key);
    return !original || original.name !== row.name || row.value !== null;
  });

  function update(key: string, patch: Partial<Row>) {
    setRows(current => current.map(row => row.key === key ? { ...row, ...patch } : row));
    setMessage(""); setError("");
  }

  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(""); setMessage("");
    try {
      const result = await api.saveEnvironment(rows.map(({ id, name, value }) => ({ id, name: name.trim(), value })));
      load(result.variables);
      refreshLlms();
      setMessage("Saved. New requests use these values.");
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  return <section className="panel environment-settings" aria-labelledby="environment-heading">
    <div className="environment-heading">
      <div>
        <h2 id="environment-heading">Environment variables</h2>
        <p className="muted small">Values stay hidden after you save them. A value saved here replaces any preconfigured value.</p>
      </div>
      <button type="button" disabled={!loaded || busy} onClick={() => {
        const key = `new-${nextKey.current++}`;
        setRows(current => [...current, { key, name: "", value: "", has_value: false, fixed: false }]);
        setMessage(""); setError("");
      }}><PlusIcon />Add variable</button>
    </div>
    <form onSubmit={save}>
      {!loaded && !error && <p className="muted small">Loading environment variables…</p>}
      {rows.length > 0 && <ul className="env-list">
        {rows.map((row, index) => <EnvironmentItem key={row.key} row={row} index={index} busy={busy}
          onChange={patch => update(row.key, patch)}
          onEdit={() => update(row.key, { editing: true })}
          onCancel={() => setRows(current => current.map(r => r.key === row.key ? saved.find(s => s.key === row.key) ?? r : r))}
          onRemove={() => { setRows(current => current.filter(r => r.key !== row.key)); setMessage(""); setError(""); }} />)}
      </ul>}
      {rows.some(row => !row.fixed) && <p className="muted small">Remove a variable and save to use its preconfigured value again, if there is one. An empty saved value means “no value”.</p>}
      {error && <p className="warn-text" role="alert">{error}</p>}
      <div className="form-actions">
        {message && !dirty && <span className="ok-text small env-status" role="status">
          <Icon><path d="m3.5 8.5 3 3 6-6.5" /></Icon>{message}
        </span>}
        {dirty && <span className="muted small">Unsaved changes</span>}
        <button type="submit" className="primary" disabled={!loaded || !dirty || busy}>{busy ? "Saving…" : "Save changes"}</button>
      </div>
    </form>
  </section>;
}
