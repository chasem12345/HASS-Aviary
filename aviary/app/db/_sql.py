"""SQL fragments every species-shaped query shares: seen/heard counts, the named and confirmed clauses."""

from __future__ import annotations
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional


__all__ = [
    "_SEEN_UNIT_IN_SPECIES",
    "_SEEN_UNIT",
    "SEEN_COUNT",
    "SEEN_COUNT_BY_SPECIES",
    "HEARD_COUNT",
    "_source_clause",
    "UNNAMED",
    "_named_clause",
    "_confirmed_clause",
]


# A "seen" is one (visit, species) pair, not one Frigate event: a bird that Frigate
# tracked as fifteen short objects was one bird, once. Events with no visit (no retained
# review item, or history from before review items) count as their own unit via -id, so
# old numbers stay exactly what they were. BirdNET rows are never grouped.
#
# Two spellings because the species-grouped queries already partition by common_name and
# need only the visit part, while ungrouped totals must keep species apart themselves.
_SEEN_UNIT_IN_SPECIES = "CASE WHEN source = 'frigate' THEN COALESCE(visit_id, -id) END"


_SEEN_UNIT = ("CASE WHEN source = 'frigate' THEN COALESCE(visit_id, -id) || '|' || "
              "LOWER(common_name) END")


SEEN_COUNT = f"COUNT(DISTINCT {_SEEN_UNIT})"


SEEN_COUNT_BY_SPECIES = f"COUNT(DISTINCT {_SEEN_UNIT_IN_SPECIES})"


HEARD_COUNT = "COALESCE(SUM(source = 'birdnet'), 0)"


# --------------------------------------------------------------------------- queries

def _source_clause(source: Optional[str], params: list) -> str:
    if source in ("frigate", "birdnet"):
        params.append(source)
        return " AND source = ?"
    return ""


# The placeholder name carried by a detection with no species. It is the absence of an
# answer, not an answer — kept in sync with ingest.is_unclassified().
UNNAMED = "bird"


def _named_clause(table: str = "detections") -> str:
    """Exclude species-less detections from anything species-shaped.

    Before external identification existed this was unnecessary: ``ignore_unclassified``
    meant a row named 'bird' was never stored, so every query could assume a real species.
    Identification deliberately stores such rows (pending, then possibly unidentifiable),
    which turned that assumption into a bug — 'bird' queued for confirmation as a species,
    took a dex number, and counted toward the species total.

    Applied to every query that answers "which species", so the rule lives in one place
    rather than being remembered independently in a dozen SQL strings.
    """
    return f" AND {table}.common_name != '{UNNAMED}' COLLATE NOCASE"


# Registry queries hide unconfirmed species while the confirmation gate is on. "Unconfirmed"
# means *absent from* species_confirmed, so this is an EXISTS test rather than a join —
# a join would risk duplicating detection rows and would need an outer join to express
# absence anyway. Callers pass only_confirmed=settings.require_species_confirmation, so with
# the gate off every query runs exactly as it did before and nothing is stranded.
def _confirmed_clause(only_confirmed: bool, table: str = "detections") -> str:
    if not only_confirmed:
        return ""
    return (f" AND EXISTS (SELECT 1 FROM species_confirmed sc"
            f" WHERE sc.common_name = {table}.common_name COLLATE NOCASE)")
