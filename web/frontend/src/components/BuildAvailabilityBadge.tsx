import { useEffect, useState } from 'react';
import type { BuildAvailability } from '../api';

export default function BuildAvailabilityBadge({ value }: { value?: BuildAvailability | null }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    setNow(Date.now() / 1000);
    if (!value?.expires_at) return;
    const timer = setTimeout(() => setNow(Date.now() / 1000), Math.max(0, value.expires_at * 1000 - Date.now()) + 50);
    const visible = () => setNow(Date.now() / 1000);
    document.addEventListener('visibilitychange', visible);
    return () => { clearTimeout(timer); document.removeEventListener('visibilitychange', visible); };
  }, [value]);
  const status = value?.status === 'checking' ? 'checking'
    : !value?.expires_at || now >= value.expires_at ? 'needs_check' : value.status;
  const labels = { checking: 'Checking stock…', available: 'All parts available', blocked: 'Parts need review', needs_check: 'Needs a stock check' };
  return <div className="build-availability" aria-live="polite">
    <span className={`badge ${status === 'blocked' ? 'warn' : ''}`}>{labels[status]}</span>
    <span className="muted small">{value?.enforced ? 'Verified before publication. Stock is not reserved.' : 'Saved before required stock checks.'}</span>
  </div>;
}
