import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import type { BuildBudget } from '../api';
import { budgetChoice, money, parseBudget } from '../budget';

export default function BudgetDialog({ initial, starting, onChoose, onCancel, returnFocus }: {
  initial: BuildBudget; starting: boolean; onChoose: (choice: BuildBudget) => Promise<void> | void;
  onCancel: () => void; returnFocus: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const restore = useRef(returnFocus);
  const id = useId();
  const [value, setValue] = useState(initial.amount_cents === null ? '' : (initial.amount_cents / 100).toFixed(2));
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const submitting = useRef(false);
  const amount = parseBudget(value);
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    if (matchMedia('(pointer: fine)').matches) input.current?.focus();
    else element.focus();
    return () => { element.close(); restore.current(); };
  }, []);
  async function choose(choice: BuildBudget) {
    if (submitting.current) return;
    submitting.current = true; setSaving(true); setError('');
    try { await onChoose(choice); }
    catch (cause) { setError((cause as Error).message); }
    finally { submitting.current = false; setSaving(false); }
  }
  function submit(event: FormEvent) {
    event.preventDefault(); event.stopPropagation();
    if (amount === null) { setError('Enter a positive USD amount with up to two decimal places.'); input.current?.focus(); return; }
    void choose(budgetChoice(amount));
  }
  return createPortal(<dialog ref={dialog} className="budget-dialog" aria-labelledby={`${id}-title`} aria-describedby={`${id}-help`}
    tabIndex={-1} onCancel={event => { event.preventDefault(); if (!saving) onCancel(); }} onKeyDown={event => {
      if (event.key !== 'Tab') return;
      const controls = [...event.currentTarget.querySelectorAll<HTMLInputElement | HTMLButtonElement>('input:not([disabled]), button:not([disabled])')];
      const first = controls[0], last = controls.at(-1), active = document.activeElement;
      if (!first) event.preventDefault();
      else if (event.shiftKey && (active === first || active === event.currentTarget)) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }
    }}>
    <form onSubmit={submit}>
      <h2 id={`${id}-title`}>{starting ? 'Set a parts budget' : 'Change parts budget'}</h2>
      <p id={`${id}-help`}>Aim as close as possible to the maximum with useful detail and solid construction. You can skip the budget.</p>
      <label htmlFor={`${id}-amount`}>Parts budget (USD)</label>
      <div className="budget-input"><span aria-hidden="true">$</span><input ref={input} id={`${id}-amount`} type="text"
        inputMode="decimal" autoComplete="off" spellCheck={false} value={value} disabled={saving}
        aria-invalid={!!error} aria-describedby={`${id}-limit ${id}-costs${error ? ` ${id}-error` : ''}`}
        onChange={event => { setValue(event.target.value); setError(''); }} placeholder="100.00" /></div>
      <p id={`${id}-limit`} className="budget-limit" aria-live="polite">{amount === null
        ? 'A $100.00 budget allows up to $105.00.'
        : `${money(amount)} budget → ${money(budgetChoice(amount).maximum_cents!)} maximum`}</p>
      <p id={`${id}-costs`} className="muted small">The target includes the 5% allowance. Service fees, shipping, and tax are separate.</p>
      <p className="muted small">This budget carries over to later edits. You can change or remove it when the build is idle.</p>
      {error && <p id={`${id}-error`} className="warn-text" role="alert">{error}</p>}
      <div className="budget-actions">
        <button type="submit" className="primary" disabled={saving}>{saving ? 'Saving…' : starting ? 'Start with budget' : 'Save budget'}</button>
        <button type="button" disabled={saving} onClick={() => void choose(budgetChoice(null))}>{starting ? 'Skip budget' : 'Remove budget'}</button>
        <button type="button" disabled={saving} onClick={onCancel}>Cancel</button>
      </div>
    </form>
  </dialog>, document.body);
}
