"""Fact verification, source-quote grounding, conflict detection/resolution, and strict contact verification."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from api_client import ApiError, EmailResult
from research.models import (
    CandidateContact,
    ConflictRecord,
    FetchedPage,
    FieldEvidence,
    SourceReliability,
    SourceType,
    VerificationStatus,
)
from research.scoring import classify_source, compute_field_confidence, source_authority_weight
from schemas import EMAIL_RE


def normalize_for_compare(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.strip().casefold().split())
    return str(value).strip().casefold()


def is_quote_grounded(quote: str, pages: list[FetchedPage]) -> bool:
    """Verify an evidence excerpt or value actually appears in fetched page content."""
    clean_q = " ".join((quote or "").strip().casefold().split())
    if not clean_q or len(clean_q) < 4:
        return False
    tokens = [t for t in re.findall(r"[a-z0-9@._\-]+", clean_q) if len(t) >= 3]
    for page in pages:
        haystack = " ".join(
            [
                page.text,
                page.title,
                page.meta_description,
                " ".join(page.headings),
                " ".join(page.emails_found),
                " ".join(page.phones_found),
                " ".join(page.linkedin_urls),
            ]
        ).casefold()
        if clean_q in haystack:
            return True
        if tokens and len(tokens) >= 3:
            matched = sum(1 for tok in tokens if tok in haystack)
            if matched / len(tokens) >= 0.7:
                return True
    return False


def build_evidence(
    field_name: str,
    value: Any,
    source_url: str,
    pages_by_url: dict[str, FetchedPage],
    *,
    excerpt: str = "",
    official_domain: str | None = None,
    event_domain: str | None = None,
    is_snippet_only: bool = False,
    publication_date: str | None = None,
) -> FieldEvidence:
    page = pages_by_url.get(source_url) or pages_by_url.get(source_url.rstrip("/"))
    if page is not None:
        stype = page.source_type
        srel = page.source_reliability
        stitle = page.title or page.domain
        pub_date = publication_date or page.publication_date
        grounded = is_quote_grounded(excerpt or str(value), [page])
    else:
        stype, srel = classify_source(
            source_url,
            official_domain=official_domain,
            event_domain=event_domain,
            is_snippet_only=is_snippet_only,
        )
        stitle = urlparse(source_url).hostname or source_url
        pub_date = publication_date
        grounded = False if pages_by_url else False

    if srel == SourceReliability.HIGH and (grounded or page is not None):
        status = VerificationStatus.VERIFIED
    elif srel == SourceReliability.MEDIUM and (grounded or page is not None):
        status = VerificationStatus.PARTIALLY_VERIFIED
    elif is_snippet_only or not source_url:
        status = VerificationStatus.REQUIRES_REVIEW
    else:
        status = VerificationStatus.PARTIALLY_VERIFIED if grounded else VerificationStatus.REQUIRES_REVIEW

    ev = FieldEvidence(
        field_name=field_name,
        value=value,
        source_title=stitle,
        source_url=source_url,
        source_type=stype,
        source_reliability=srel,
        publication_date=pub_date,
        excerpt=(excerpt or str(value))[:300],
        verification_status=status,
    )
    ev.confidence_score = compute_field_confidence([ev], status)
    return ev


def resolve_field_candidates(
    field_name: str,
    evidences: list[FieldEvidence],
    *,
    existing_verified_value: Any = None,
) -> tuple[Any, VerificationStatus, list[FieldEvidence], ConflictRecord | None]:
    """Evaluate multiple source evidences, preserving conflicts and ranking by authority & recency."""
    non_empty = [e for e in evidences if e.value not in (None, "", [], {})]
    if not non_empty:
        return None, VerificationStatus.UNAVAILABLE, [], None

    # Group distinct normalized values.
    groups: dict[str, list[FieldEvidence]] = {}
    for ev in non_empty:
        key = normalize_for_compare(ev.value)
        groups.setdefault(key, []).append(ev)

    # Check cached verified value before early single-group return.
    if existing_verified_value is not None:
        norm_existing = normalize_for_compare(existing_verified_value)
        if any(normalize_for_compare(e.value) != norm_existing for e in non_empty):
            candidates_dump = [
                {
                    "value": ev.value,
                    "source_url": ev.source_url,
                    "source_type": ev.source_type.value,
                    "source_reliability": ev.source_reliability.value,
                    "publication_date": ev.publication_date,
                    "excerpt": ev.excerpt,
                }
                for ev in non_empty
            ] + [{
                "value": existing_verified_value,
                "source_type": "CACHED_VERIFIED",
                "source_reliability": "HIGH",
                "source_url": "",
                "publication_date": None,
                "excerpt": "Previously verified cached field value",
            }]
            conflict = ConflictRecord(
                field_name=field_name,
                candidates=candidates_dump,
                resolution_status="UNRESOLVED_CONFLICT",
                chosen_value=existing_verified_value,
                rationale="Conflict with previously verified cached value; preserved existing verified value for review.",
            )
            marked = [
                e.model_copy(
                    update={
                        "verification_status": VerificationStatus.CONFLICTING,
                        "conflict_notes": ["Conflicts with previously verified cached value"],
                    }
                )
                for e in non_empty
            ]
            return existing_verified_value, VerificationStatus.CONFLICTING, marked, conflict

    if len(groups) == 1:
        # All sources agree.
        evs = non_empty
        has_high = any(e.source_reliability == SourceReliability.HIGH for e in evs)
        medium_domains = {
            urlparse(e.source_url).hostname
            for e in evs
            if e.source_reliability == SourceReliability.MEDIUM and e.source_url
        }
        if has_high or len(medium_domains) >= 2:
            status = VerificationStatus.VERIFIED
        elif any(e.source_reliability == SourceReliability.MEDIUM for e in evs):
            status = VerificationStatus.PARTIALLY_VERIFIED
        else:
            status = VerificationStatus.REQUIRES_REVIEW
        best_ev = max(evs, key=lambda e: source_authority_weight(e.source_type))
        conf = compute_field_confidence(evs, status)
        updated_evs = [e.model_copy(update={"verification_status": status, "confidence_score": conf}) for e in evs]
        return best_ev.value, status, updated_evs, None

    # Multiple disagreeing values exist!
    best_per_group: list[tuple[float, str, FieldEvidence]] = []
    for _k, grp in groups.items():
        top = max(grp, key=lambda e: (source_authority_weight(e.source_type), e.publication_date or ""))
        w = source_authority_weight(top.source_type)
        best_per_group.append((w, top.publication_date or "", top))

    best_per_group.sort(key=lambda x: (x[0], x[1]), reverse=True)
    top_w, top_date, top_ev = best_per_group[0]
    sec_w, sec_date, sec_ev = best_per_group[1]

    candidates_dump = [
        {
            "value": ev.value,
            "source_url": ev.source_url,
            "source_type": ev.source_type.value,
            "source_reliability": ev.source_reliability.value,
            "publication_date": ev.publication_date,
            "excerpt": ev.excerpt,
        }
        for _, _, ev in best_per_group
    ]

    if top_w - sec_w >= 0.15:
        conflict = ConflictRecord(
            field_name=field_name,
            candidates=candidates_dump,
            resolution_status="RESOLVED_BY_AUTHORITY",
            chosen_value=top_ev.value,
            rationale=(
                f"Resolved in favor of higher-authority source ({top_ev.source_type.value}) "
                f"over {sec_ev.source_type.value}."
            ),
        )
        status = VerificationStatus.VERIFIED if top_ev.source_reliability == SourceReliability.HIGH else VerificationStatus.PARTIALLY_VERIFIED
        conf = compute_field_confidence([top_ev], status)
        marked = [
            e.model_copy(
                update={
                    "verification_status": status if e is top_ev else VerificationStatus.CONFLICTING,
                    "confidence_score": conf,
                    "conflict_notes": [conflict.rationale],
                }
            )
            for e in non_empty
        ]
        return top_ev.value, status, marked, conflict

    if top_date and sec_date and top_date != sec_date:
        conflict = ConflictRecord(
            field_name=field_name,
            candidates=candidates_dump,
            resolution_status="RESOLVED_BY_RECENCY",
            chosen_value=top_ev.value,
            rationale=f"Equal-authority sources disagreed; selected more recent publication date ({top_date} > {sec_date}).",
        )
        status = VerificationStatus.PARTIALLY_VERIFIED
        conf = compute_field_confidence([top_ev], status)
        marked = [
            e.model_copy(
                update={
                    "verification_status": status if e is top_ev else VerificationStatus.CONFLICTING,
                    "confidence_score": conf,
                    "conflict_notes": [conflict.rationale],
                }
            )
            for e in non_empty
        ]
        return top_ev.value, status, marked, conflict

    # Equal authority and unable to resolve cleanly -> mark CONFLICTING
    conflict = ConflictRecord(
        field_name=field_name,
        candidates=candidates_dump,
        resolution_status="UNRESOLVED_CONFLICT",
        chosen_value=None,
        rationale="Equal-authority sources disagreed with no clear recency winner.",
    )
    conf = compute_field_confidence(non_empty, VerificationStatus.CONFLICTING)
    marked = [
        e.model_copy(
            update={
                "verification_status": VerificationStatus.CONFLICTING,
                "confidence_score": conf,
                "conflict_notes": [conflict.rationale],
            }
        )
        for e in non_empty
    ]
    return None, VerificationStatus.CONFLICTING, marked, conflict


async def verify_contact_candidate(
    candidate: dict[str, Any],
    *,
    company_name: str,
    official_domain: str | None,
    pages_by_url: dict[str, FetchedPage],
    api_client: Any,
) -> CandidateContact:
    """Enforce strict AI contact rules: name + designation + grounded email + VALID instant_verify."""
    name = " ".join(str(candidate.get("contact_name") or "").split())
    designation = " ".join(str(candidate.get("designation") or "").split()) or None
    email = " ".join(str(candidate.get("official_email") or "").split()) or None
    phone = " ".join(str(candidate.get("phone_number") or "").split()) or None
    linkedin = " ".join(str(candidate.get("linkedin_profile") or "").split()) or None
    prefix = " ".join(str(candidate.get("prefix") or "").split()) or None
    src_url = str(candidate.get("source_url") or "")
    excerpt = str(candidate.get("evidence_quote") or "")
    page = pages_by_url.get(src_url) or pages_by_url.get(src_url.rstrip("/"))
    stype = page.source_type if page else SourceType.UNKNOWN
    stitle = page.title if page else ""

    def _reject(reason: str, ev_status: str = "UNCHECKED", ev_reason: str = "") -> CandidateContact:
        return CandidateContact(
            prefix=prefix,
            contact_name=name or "(unnamed)",
            designation=designation,
            official_email=email,
            phone_number=phone,
            linkedin_profile=linkedin,
            source_url=src_url,
            source_title=stitle,
            source_type=stype,
            excerpt=excerpt[:250],
            company_verified=False,
            email_verification_status=ev_status,
            email_verification_reason=ev_reason,
            included_in_payload=False,
            exclusion_reason=reason,
        )

    if not name:
        return _reject("Missing contact_name.")
    if not designation:
        return _reject("Missing designation / role (all 3 required fields must be verified for AI contacts).")
    if not email or not EMAIL_RE.match(email):
        return _reject("Missing or invalid official_email (all 3 required fields must be verified for AI contacts).")

    # Must NOT accept pattern-guessed emails: email MUST appear verbatim in fetched sources!
    email_grounded = False
    all_pages = list(pages_by_url.values())
    for p in all_pages:
        if any(e.casefold() == email.casefold() for e in p.emails_found) or email.casefold() in p.text.casefold():
            email_grounded = True
            break
    if all_pages and not email_grounded:
        return _reject("Email address was not grounded in any fetched source (pattern-guessed emails are forbidden).")

    # Validate company association: either page is on official domain or page text mentions company name or email matches official domain.
    email_domain = email.split("@", 1)[1].lower()
    norm_official = (official_domain or "").lower().removeprefix("www.")
    company_tokens = [t for t in re.findall(r"[a-z0-9]+", company_name.casefold()) if len(t) >= 3]
    company_verified = False
    if norm_official and (email_domain == norm_official or email_domain.endswith("." + norm_official)):
        company_verified = True
    if page is not None:
        if norm_official and (page.domain.removeprefix("www.") == norm_official or page.domain.endswith("." + norm_official)):
            company_verified = True
        elif company_tokens and all(tok in page.text.casefold() for tok in company_tokens[:2]):
            company_verified = True
    if not company_verified:
        return _reject("Could not verify contact association with the target company.")

    # Call Market Intel instant_verify integration; require VALID.
    try:
        res: EmailResult = await api_client.instant_verify(email)
    except ApiError as exc:
        return _reject(f"Email verification API error: {exc}", ev_status="ERROR", ev_reason=str(exc))

    if not res.ok:
        return _reject(
            f"Email verification returned {res.status}: {res.reason}",
            ev_status=res.status,
            ev_reason=res.reason,
        )

    return CandidateContact(
        prefix=prefix,
        contact_name=name,
        designation=designation,
        official_email=email,
        phone_number=phone,
        linkedin_profile=linkedin,
        source_url=src_url,
        source_title=stitle,
        source_type=stype,
        excerpt=excerpt[:250],
        company_verified=True,
        email_verification_status=res.status,
        email_verification_reason=res.reason,
        included_in_payload=True,
        exclusion_reason=None,
    )
