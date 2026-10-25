import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api, type ModelQuote, type QuoteSummary, timeAgo } from '../api';

const money = (value: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value / 100);
const labels = { available: 'Available', out_of_stock: 'Out of stock', unlisted: 'Not listed', unmapped: 'Needs a match' };

export default function PartsQuote({ modelUrl, initial }: { modelUrl: string; initial?: QuoteSummary | null }) {
  const [summary, setSummary] = useState(initial);
  const [open, setOpen] = useState(false);
  const [quote, setQuote] = useState<ModelQuote | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [availableOnly, setAvailableOnly] = useState(false);
  const [cartStatus, setCartStatus] = useState('');
  const [cartItems, setCartItems] = useState<{elementId:string; requested:number; added:number; missing:number; in_bag:number}[]>([]);
  const [helper, setHelper] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const pending = useRef<string | null>(null);
  useEffect(() => setSummary(initial), [initial]);
  useEffect(() => {
    if (summary && 'id' in summary && !summary.stale) return;
    let cancelled=false;
    const timer=setTimeout(async () => { try { const next=await api.quoteSummary(modelUrl); if (!cancelled) setSummary(next); } catch { if (!cancelled) setSummary({revision:'pending',status:'pending'}); } },4000);
    return () => {cancelled=true;clearTimeout(timer);};
  },[summary,modelUrl]);
  useEffect(() => {
    function receive(event: MessageEvent) {
      if (event.source !== window || event.origin !== location.origin || event.data?.source !== 'lego-nova-helper') return;
      if (event.data.type === 'ready') setHelper(true);
      if (event.data.type === 'result' && event.data.intentId === pending.current) {
        setCartStatus(event.data.message || 'Review the result in the LEGO tab.'); setCartItems(event.data.items || []); setBusy(false);
      }
    }
    window.addEventListener('message', receive);
    window.postMessage({ source: 'lego-nova-app', type: 'ping' }, location.origin);
    return () => window.removeEventListener('message', receive);
  }, []);
  useEffect(() => {
    if (open) dialog.current?.showModal();
    else { dialog.current?.close(); opener.current?.focus(); }
  }, [open]);

  async function refresh() {
    setBusy(true); setError(''); setAvailableOnly(false); setCartStatus(''); setCartItems([]); pending.current=null;
    try {
      const next = await api.createQuote(modelUrl, true);
      setQuote(next); setSummary(next);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function review() { setOpen(true); await refresh(); }
  async function add(event: React.MouseEvent) {
    // Automatic model publication and synthetic DOM clicks cannot create a cart request.
    if (!event.isTrusted || !quote || busy || !helper) return;
    setBusy(true); setError('');
    try {
      const intent = await api.createCartIntent(quote, availableOnly);
      pending.current = intent.id;
      setCartStatus('The helper is checking your reviewed list and LEGO bag.');
      window.postMessage({ source: 'lego-nova-app', type: 'cart-intent', intent }, location.origin);
    } catch (e) { setError((e as Error).message); setBusy(false); }
  }
  const importCount = quote ? new Set(quote.lines.filter(l => l.state==='available' && l.unit_price!==null && !l.issue && l.element_id).map(l=>l.element_id)).size : 0;
  const priced = summary && 'id' in summary ? summary : null;
  const complete = quote && quote.purchasable_quantity === quote.total_quantity;
  const ready = quote && !quote.stale && Date.now() / 1000 - quote.created_at < 600;
  return <div className="parts-quote">
    <div className="quote-summary">
      <strong>{priced ? (priced.priced_quantity ? money(priced.subtotal ?? priced.known_subtotal) : 'Price unavailable') : 'Parts pricing'}</strong>
      <span className="muted small">{priced ? `${priced.subtotal === null ? 'Known prices · ' : ''}${priced.purchasable_quantity}/${priced.total_quantity} pieces available · ${priced.unresolved_lines} rows need review` : 'US LEGO Pick a Brick · USD'}</span>
      {priced && <span className="muted small">{priced.stale ? 'Prices need an update' : `Checked ${timeAgo(priced.created_at)}`}</span>}
      {priced && <span className="muted small">{priced.fees.length ? priced.fees.map(f => `${f.name}: ${money(f.amount)}`).join(' · ') : 'Service fees need checkout confirmation'}</span>}
    </div>
    <button ref={opener} type="button" onClick={review}>Review parts and cost</button>
    {open && createPortal(<dialog ref={dialog} className="parts-dialog" aria-labelledby="parts-title" onCancel={() => setOpen(false)}>
      <header className="modal-head"><strong id="parts-title">Parts and cost</strong><button className="icon-button" aria-label="Close parts review" onClick={() => setOpen(false)}>×</button></header>
      <div className="parts-review">
        {quote && <>
          <div className="quote-total"><strong>{quote.priced_quantity ? money(quote.subtotal ?? quote.known_subtotal) : 'Price unavailable'}</strong><span>{quote.subtotal === null ? 'Known parts subtotal' : 'Parts subtotal'} · USD</span></div>
          <p>{quote.purchasable_quantity} of {quote.total_quantity} pieces available. {quote.unresolved_lines} parts-list rows need review.</p>
          <p className="muted small">{quote.fees.length ? quote.fees.map(f => `${f.name}: ${money(f.amount)}`).join(' · ') : 'Service fees need checkout confirmation.'} LEGO calculates shipping and tax at checkout.</p>
          <div className="parts-table-wrap"><table className="parts-table"><thead><tr><th>Part / colour</th><th>Quantity</th><th>Element</th><th>Price</th><th>Status</th></tr></thead><tbody>
            {quote.lines.map(line => <tr key={`${line.part}-${line.color}`}>
              <td><strong>{line.name || line.part}</strong><small>{line.part} · LDraw colour {line.color}</small></td>
              <td>{line.quantity}</td><td>{line.element_id || 'Unresolved'}</td>
              <td>{line.line_total === null ? 'Unknown' : money(line.line_total)}</td>
              <td>{labels[line.state]}{line.stale && <small>Needs price refresh</small>}{line.issue && <small>{line.issue.replaceAll('_', ' ')}</small>}
                {line.alternatives.map(a => <small key={a.element_id}>Possible {a.relationship.replaceAll('_', ' ')}: {a.part}, element {a.element_id}. Review the model before replacing.</small>)}</td>
            </tr>)}
          </tbody></table></div>
          {!complete && <label className="available-choice"><input type="checkbox" checked={availableOnly} onChange={e => setAvailableOnly(e.target.checked)} /> Add only verified, available pieces. Keep all other pieces unresolved.</label>}
          <p className="muted small">Identity data: <a href="https://rebrickable.com/downloads/" target="_blank" rel="noreferrer">Rebrickable</a>. Prices and stock: LEGO US. No replacements apply automatically.</p>
        </>}
        <div className="parts-actions">
          <button onClick={refresh} disabled={busy}>{busy && !pending.current ? 'Checking prices…' : 'Refresh prices'}</button>
          <button onClick={add} disabled={busy || !ready || (!complete && !availableOnly) || !helper}>Add to LEGO bag</button>
          {quote && ready && (complete || availableOnly) && Array.from({ length: Math.ceil(importCount / 400) }, (_, batch) => <a key={batch} className="button" href={`/api/quotes/${quote.id}/import/${batch}?available_only=${availableOnly}`} download>Download LEGO import{importCount > 400 ? ` ${batch + 1}` : ''}</a>)}
        </div>
        {!helper && <p className="muted small"><a href="/api/chrome-helper/download" download>Download the private Chrome helper</a> to fill the bag. You can also import the downloaded file on LEGO.</p>}
        {cartStatus && <p role="status">{cartStatus}</p>}
        {!!cartItems.length && <ul className="cart-results">{cartItems.map(item => <li key={item.elementId}>Element {item.elementId}: {item.in_bag ?? 0} confirmed in bag; {item.added} added to parts list; {item.missing} missing.</li>)}</ul>}
        {error && <p className="warn-text" role="alert">{error}</p>}
        <p className="muted small">Cart actions require your request. Checkout stays on LEGO’s website.</p>
      </div>
    </dialog>, document.body)}
  </div>;
}
