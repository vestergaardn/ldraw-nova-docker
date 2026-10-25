"""Server-owned parts budgets. Quotes remain independent of the spending rule."""
from typing import Literal, TypedDict

UNSET = object()
MAX_CENTS = (2**53 - 1) // 105  # exact arithmetic in the browser as well as Python


class BuildBudget(TypedDict):
    status: Literal['pending', 'skipped', 'set']
    amount_cents: int | None
    maximum_cents: int | None
    currency: Literal['USD']
    basis: Literal['parts']
    allowance_percent: int


class BudgetAssessment(TypedDict):
    budget: BuildBudget
    status: str
    subtotal: int | None
    remaining_cents: int | None
    remaining_allowance_cents: int | None
    excess_cents: int | None
    largest_costs: list[dict]
    revision: str
    verification_quote: str
    checked_at: float | None


def normalize(value=None, *, default='skipped') -> BuildBudget:
    if value is None:
        value = {'status': default}
    if not isinstance(value, dict) or value.get('status') not in {'pending', 'skipped', 'set'}:
        raise ValueError('Choose a parts budget or skip it.')
    status, amount = value['status'], value.get('amount_cents')
    if status == 'set':
        if type(amount) is not int or not 1 <= amount <= MAX_CENTS:
            raise ValueError('Use a positive parts budget in whole USD cents.')
    elif amount is not None:
        raise ValueError('A skipped or unanswered budget cannot contain an amount.')
    result = dict(status=status, amount_cents=amount, maximum_cents=amount * 105 // 100 if amount else None,
                  currency='USD', basis='parts', allowance_percent=5)
    if any(key in value and value[key] != result[key] for key in ('maximum_cents', 'currency', 'basis', 'allowance_percent')):
        raise ValueError('Parts budgets use USD and a fixed 5% allowance.')
    return result


def assess(budget, quote) -> BudgetAssessment:
    budget = normalize(budget)
    lines = quote.get('lines', [])
    complete = bool(lines)
    subtotal, costs = 0, []
    for line in lines:
        price, quantity = line.get('unit_price'), line.get('quantity')
        if type(price) is not int or price < 0 or type(quantity) is not int or quantity < 1:
            complete = False
            continue
        total = price * quantity
        subtotal += total
        complete = complete and line.get('line_total') == total
        costs.append({key: line.get(key) for key in ('part', 'color', 'name', 'quantity', 'unit_price')} | {'line_total': total})
    complete = complete and quote.get('subtotal') == subtotal and quote.get('currency') == 'USD'
    subtotal = subtotal if complete else None
    target, maximum = budget['amount_cents'], budget['maximum_cents']
    status = ('unbudgeted' if target is None else 'unknown' if subtotal is None else
              'within_budget' if subtotal <= target else 'within_allowance' if subtotal <= maximum else 'over_budget')
    return dict(budget=budget, status=status, subtotal=subtotal,
                remaining_cents=target-subtotal if target is not None and subtotal is not None else None,
                remaining_allowance_cents=maximum-subtotal if maximum is not None and subtotal is not None else None,
                excess_cents=max(0, subtotal-maximum) if maximum is not None and subtotal is not None else None,
                largest_costs=sorted(costs, key=lambda line: (-line['line_total'], str(line['part']), str(line['color'])))[:8],
                revision=quote['revision'], verification_quote=quote['id'], checked_at=quote.get('checked_at'))


def problems(assessment):
    if assessment['status'] not in {'unknown', 'over_budget'}:
        return []
    return [dict(issue='budget_exceeded' if assessment['status'] == 'over_budget' else 'budget_price_incomplete',
                 budget=assessment['budget'], subtotal=assessment['subtotal'], excess_cents=assessment['excess_cents'],
                 reason='The parts cost exceeds the permitted maximum.' if assessment['status'] == 'over_budget'
                        else 'Every required part needs a verified price before checking the budget.')]


def require(budget, quote):
    assessment = assess(budget, quote)
    if problems(assessment):
        raise ValueError('The parts budget check failed. Repair and price the draft again.')
    return assessment


def continuation(budget):
    budget = normalize(budget)
    if budget['status'] != 'set':
        return 'Send a message to continue.'
    return (f"Unfinished drafts stay saved. The ${budget['maximum_cents']/100:.2f} parts maximum still applies. "
            'Continue the draft, request a simpler design, or change the budget before continuing.')


def instruction(budget):
    budget = normalize(budget)
    if budget['status'] != 'set':
        return '\nNo parts spending limit was selected. All stock and publication checks still apply.'
    return (f"\nParts budget: ${budget['amount_cents']/100:.2f} USD. "
            f"The absolute permitted maximum is ${budget['maximum_cents']/100:.2f}, including the fixed 5% allowance. "
            'Only LEGO parts count; service fees, shipping and tax are separate. Aim within the target. '
            'Use the allowance only for a meaningful design improvement. Preserve the requested concept, proportions and defining details. '
            'Plan scale and major assemblies against verified prices before final rendering. Compare colours, unit prices and repeated quantities. '
            'Use search_available_parts with max_unit_price when helpful. Check each substantial assembly with check_build_parts. '
            'Reduce expensive construction or scale before sacrificing defining features. Spend spare budget only when it improves the design. '
            'Do not add unnecessary pieces to use up the budget. If a check exceeds the maximum, redesign and check again. '
            'Explain cost-driven changes and the final parts cost. If no suitable design fits within runtime limits, retain the draft and explain '
            'whether a higher budget or a simpler scope could help. Never claim a global optimum or bypass publication checks.')
