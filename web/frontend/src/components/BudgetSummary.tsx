import type { BudgetAssessment, QuoteSummary } from '../api';
import { money } from '../budget';

export default function BudgetSummary({ assessment, quote }: { assessment?: BudgetAssessment | null; quote?: QuoteSummary | null }) {
  if (!assessment || assessment.budget.status !== 'set') return null;
  const budget = assessment.budget;
  const priced = quote && 'id' in quote && quote.revision === assessment.revision ? quote : null;
  const over = priced?.subtotal != null && priced.subtotal > budget.maximum_cents!;
  return <div className="budget-summary" data-budget-status={over ? 'over_budget' : assessment.status}>
    <strong>Parts budget {money(budget.amount_cents!)} · maximum {money(budget.maximum_cents!)}</strong>
    <span>At completion: {assessment.subtotal === null ? 'Price unavailable' : money(assessment.subtotal)}
      {assessment.status === 'within_allowance' ? ' · Used the 5% allowance' : ' · Within budget'}</span>
    {priced && <span className={over ? 'warn-text' : 'muted'}>{priced.subtotal === null ? 'Latest prices are incomplete.' :
      `Last checked parts cost: ${money(priced.subtotal)}${over ? ` · ${money(priced.subtotal - budget.maximum_cents!)} above the maximum` :
        priced.subtotal > budget.amount_cents! ? ' · Within the 5% allowance' : ' · Within budget'}`}</span>}
    <span className="muted">Service fees, shipping, and tax are separate. Saved models keep their original budget record.</span>
  </div>;
}
