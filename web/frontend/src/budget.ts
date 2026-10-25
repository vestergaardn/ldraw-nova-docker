import type { BuildBudget, TurnOptions } from './api';

export const MAX_BUDGET_CENTS = Math.floor(Number.MAX_SAFE_INTEGER / 105);
export const money = (cents: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(cents / 100);
export function budgetChoice(amount: number | null, pending = false): BuildBudget {
  return { status: amount === null ? (pending ? 'pending' : 'skipped') : 'set', amount_cents: amount,
    maximum_cents: amount === null ? null : Math.floor(amount * 105 / 100), currency: 'USD', basis: 'parts', allowance_percent: 5 };
}
export function parseBudget(text: string): number | null {
  const value = text.trim();
  if (!/^(?:\d+(?:\.\d{0,2})?|\.\d{1,2})$/.test(value)) return null;
  const [whole, fraction = ''] = value.split('.');
  const cents = Number(whole || '0') * 100 + Number(fraction.padEnd(2, '0'));
  return Number.isSafeInteger(cents) && cents > 0 && cents <= MAX_BUDGET_CENTS ? cents : null;
}
export function needsBudget(budget: BuildBudget, options: TurnOptions): boolean {
  return budget.status === 'pending' && options.mode === 'agent' && options.permissions !== 'read_only';
}
