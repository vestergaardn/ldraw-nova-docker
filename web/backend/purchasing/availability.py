"""Read-only purchasing checks for construction and mandatory publication."""
import asyncio
import time

from . import service
from .domain import MAX_AGE, PUBLICATION_MAX_AGE, publication_problems


async def check(snapshot, *, refresh=False, final=False, database=None, provider=None):
    quote = await service.quote_model(snapshot, refresh=refresh, strict=True,
                                     max_age=PUBLICATION_MAX_AGE if final else MAX_AGE,
                                     database=database, provider=provider)
    problems = publication_problems(quote, max_age=PUBLICATION_MAX_AGE if final else MAX_AGE)
    return quote, problems


def feedback(quote, problems):
    return dict(status='blocked' if problems else 'available', revision=quote['revision'],
                verification_quote=quote['id'], checked_at=quote['checked_at'],
                total_quantity=quote['total_quantity'], purchasable_quantity=quote['purchasable_quantity'],
                coverage=quote['coverage'], currency='USD', subtotal=quote['subtotal'],
                problems=problems[:50], problem_count=len(problems),
                next_action=('Use search_available_parts, change the draft, then check_build_parts again.' if problems
                             else 'Continue geometry and visual review. publish_model always refreshes stock after rendering.'),
                note='Availability is an observation, not a stock reservation. No cart action occurred.')
