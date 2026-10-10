"""
Confidence model (0-100). Same idea as the old engine's score (edge size,
spread, book count, repeated confirmations), rebuilt with one change: it also
rewards agreement between books, because a fair value that four books agree on
is worth more than one where they disagree by five points.

Components (max points):
    books        30   10 per independent book, capped
    agreement    25   falls to 0 as stdev across books reaches edge_max_book_dispersion
    edge         25   full marks at 2x the minimum edge
    confirmations 10  5 per earlier scan in the window that already showed the edge
    spread       10   falls to 0 at the Scout's max spread
"""

from __future__ import annotations


def confidence_score(*, n_books: int, dispersion: float, edge_cents: float,
                     confirmations: int, spread_cents: int | None, settings) -> tuple[int, dict]:
    books = min(30.0, 10.0 * n_books)
    max_disp = max(settings.edge_max_book_dispersion, 1e-9)
    agreement = 25.0 * max(0.0, 1.0 - dispersion / max_disp) if n_books > 1 else 0.0
    edge = 25.0 * max(0.0, min(1.0, edge_cents / (2.0 * settings.edge_min_cents)))
    confirm = min(10.0, 5.0 * confirmations)
    if spread_cents is None:
        spread = 0.0
    else:
        spread = 10.0 * max(0.0, 1.0 - spread_cents / max(settings.scout_max_spread_cents, 1))
    parts = {"books": round(books, 1), "agreement": round(agreement, 1), "edge": round(edge, 1),
             "confirmations": round(confirm, 1), "spread": round(spread, 1)}
    return int(round(sum(parts.values()))), parts
