"""Pure business-rule checks (no I/O). Network-dependent checks live in workflow.py."""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable

from schemas import OFFERING_FOR_PRIMARY, ProspectInput

SIMILARITY_THRESHOLD = 0.85


@dataclass(frozen=True)
class EmailTarget:
    kind: str            # "company" | "contact"
    label: str           # human readable, e.g. 'Key contact "Arun Kumar"'
    email: str
    contact_index: int | None = None


def collect_email_targets(p: ProspectInput) -> list[EmailTarget]:
    targets: list[EmailTarget] = []
    if p.official_email_address:
        targets.append(EmailTarget("company", "Company email", p.official_email_address))
    for i, c in enumerate(p.key_contacts):
        if c.official_email:
            targets.append(EmailTarget("contact", f'Key contact "{c.contact_name}"', c.official_email, i))
    return targets


def referenced_company_ids(p: ProspectInput) -> set[str]:
    """Every existing-company UUID the payload relies on (must be verified as visible)."""
    ids = {str(u) for u in p.parent_companies + p.merging_companies + p.dissolved_companies}
    ids |= {str(a.acquiring_company) for a in p.acquiring_companies_data}
    ids |= {str(s.company) for s in p.shareholdings if s.company}
    return ids


def business_rule_errors(p: ProspectInput) -> list[str]:
    errs: list[str] = []

    # Hierarchy
    if p.company_structure in ("Branch", "Subsidiary") and not p.parent_companies:
        errs.append(f"parent_companies: at least one existing parent company ID is required for a {p.company_structure}.")
    if p.company_structure == "Parent" and p.parent_companies:
        errs.append("parent_companies: a company with structure 'Parent' cannot have parent companies.")
    errs += _duplicates("parent_companies", [str(u) for u in p.parent_companies])
    errs += _duplicates("merging_companies", [str(u) for u in p.merging_companies])
    errs += _duplicates("dissolved_companies", [str(u) for u in p.dissolved_companies])
    errs += _duplicates("acquiring_companies_data", [str(a.acquiring_company) for a in p.acquiring_companies_data])

    # Operational-status conditional rules
    if p.operational_status == "Acquired" and not p.acquiring_companies_data:
        errs.append("acquiring_companies_data: required when operational_status is 'Acquired'.")
    if p.operational_status != "Acquired" and p.acquiring_companies_data:
        errs.append("acquiring_companies_data: only allowed when operational_status is 'Acquired'.")
    if p.operational_status == "Merged" and not p.merging_companies:
        errs.append("merging_companies: required when operational_status is 'Merged'.")
    if p.operational_status != "Merged" and (p.merging_companies or p.dissolved_companies):
        errs.append("merging_companies/dissolved_companies: only allowed when operational_status is 'Merged'.")
    overlap = {str(u) for u in p.merging_companies} & {str(u) for u in p.dissolved_companies}
    if overlap:
        errs.append("A company cannot be both merging and dissolved: " + ", ".join(sorted(overlap)))

    # Offerings
    seen = set()
    for i, o in enumerate(p.offerings_data):
        key = (o.offering_type, o.name.casefold())
        if key in seen:
            errs.append(f"offerings_data[{i}]: duplicate offering '{o.offering_type}: {o.name}'.")
        seen.add(key)
    wanted = OFFERING_FOR_PRIMARY.get(p.primary_offering_type)
    for i, o in enumerate(p.offerings_data):
        if wanted and o.offering_type != wanted:
            errs.append(f"offerings_data[{i}]: type '{o.offering_type}' does not match primary_offering_type "
                        f"'{p.primary_offering_type}' (expected '{wanted}').")

    # Key contacts
    names = set()
    for i, c in enumerate(p.key_contacts):
        k = (c.contact_name.casefold(), (c.official_email or "").casefold())
        if k in names:
            errs.append(f"key_contacts[{i}]: duplicate contact '{c.contact_name}'.")
        names.add(k)

    # Shareholdings
    total = 0.0
    for i, s in enumerate(p.shareholdings):
        total += s.share_percentage
        if s.holder_type == "Individual" and s.contact_index is not None and s.contact_index >= len(p.key_contacts):
            errs.append(f"shareholdings[{i}]: contact_index {s.contact_index} does not match any key contact in this payload.")
        if s.holder_type == "Government":
            errs.append(f"shareholdings[{i}]: government entity references cannot be verified through the permitted "
                        "read APIs, so this shareholding was rejected rather than guessed.")
    if total > 100.0001:
        errs.append(f"shareholdings: total share percentage is {total:g}, which exceeds 100.")

    if p.market_event_ids:
        errs.append("market_event_ids: market events cannot be verified through the permitted read APIs, "
                    "so they were rejected rather than guessed. Remove the field.")
    return errs


def _duplicates(field: str, items: Iterable[str]) -> list[str]:
    items = list(items)
    dupes = sorted({x for x in items if items.count(x) > 1})
    return [f"{field}: ID {d} is repeated." for d in dupes]


def find_duplicates(company_name: str, records: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Split visible records into (exact case-insensitive matches, similar-name matches)."""
    target = " ".join(company_name.split()).casefold()
    exact, similar = [], []
    for r in records:
        name = " ".join(str(r.get("company_name", "")).split()).casefold()
        if not name:
            continue
        if name == target:
            exact.append(r)
        elif target in name or name in target or SequenceMatcher(None, target, name).ratio() >= SIMILARITY_THRESHOLD:
            similar.append(r)
    return exact, similar
