"""Budget-prioritized 6-section company data extraction and field-level cache reuse."""
from __future__ import annotations

import re
from typing import Any

from api_client import ApiError
from config import Settings
from research.cache import ResearchCache
from research.contacts import research_key_contacts, verify_company_email
from research.corporate_structure import resolve_corporate_structure
from research.models import ConflictRecord, FetchedPage, FieldEvidence, ResearchBudget, VerificationStatus
from research.offerings import extract_industries_and_offerings
from research.ownership import resolve_ownership
from research.prompts import (
    conflict_resolution_prompt,
    corporate_structure_prompt,
    general_info_prompt,
)
from research.provider_router import ProviderRouter
from research.security import format_sources_for_prompt
from research.verifier import build_evidence, is_quote_grounded, resolve_field_candidates
from schemas import EMAIL_RE, PHONE_RE

ALL_SECTIONS = (
    "market_offerings",
    "general_information",
    "corporate_structure",
    "shareholding_ownership",
    "market_events",
    "key_contacts",
)

VOLATILE_FIELDS = frozenset({
    "key_contacts",
    "official_email_address",
    "operational_status",
    "shareholdings",
    "market_event_ids",
})


def _extract_deterministic_general_info(
    company_name: str,
    country_hint: str | None,
    official_website: str | None,
    official_domain: str | None,
    pages: list[FetchedPage],
) -> dict[str, list[FieldEvidence]]:
    """Stage 1 deterministic extraction for General Information from JSON-LD, meta, and official pages."""
    pages_by_url = {p.final_url: p for p in pages}
    for p in pages:
        pages_by_url.setdefault(p.final_url.rstrip("/"), p)

    candidates: dict[str, list[FieldEvidence]] = {
        "company_name": [],
        "country_head_office": [],
        "complete_address": [],
        "official_phone_number": [],
        "official_email_address": [],
        "official_website_url": [],
        "linkedin_company_page": [],
    }

    if official_website:
        src = pages[0].final_url if pages else official_website
        candidates["official_website_url"].append(
            build_evidence(
                "official_website_url",
                official_website,
                src,
                pages_by_url,
                excerpt=f"Resolved official website {official_website}",
                official_domain=official_domain,
            )
        )

    for p in pages:
        for obj in p.json_ld:
            t = str(obj.get("@type") or "").casefold()
            if any(k in t for k in ("organization", "corporation", "localbusiness")):
                if isinstance(obj.get("name"), str) and obj["name"].strip():
                    candidates["company_name"].append(
                        build_evidence(
                            "company_name",
                            obj["name"].strip()[:255],
                            p.final_url,
                            pages_by_url,
                            excerpt=obj["name"].strip(),
                            official_domain=official_domain,
                        )
                    )
                addr = obj.get("address")
                if isinstance(addr, dict):
                    parts = [
                        str(addr.get(k) or "").strip()
                        for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")
                    ]
                    parts = [x for x in parts if x]
                    if parts:
                        candidates["complete_address"].append(
                            build_evidence(
                                "complete_address",
                                ", ".join(parts),
                                p.final_url,
                                pages_by_url,
                                excerpt=", ".join(parts),
                                official_domain=official_domain,
                            )
                        )
                    c_country = addr.get("addressCountry")
                    if isinstance(c_country, dict):
                        c_country = c_country.get("name")
                    if isinstance(c_country, str) and c_country.strip():
                        candidates["country_head_office"].append(
                            build_evidence(
                                "country_head_office",
                                c_country.strip()[:100],
                                p.final_url,
                                pages_by_url,
                                excerpt=c_country.strip(),
                                official_domain=official_domain,
                            )
                        )
                elif isinstance(addr, str) and addr.strip():
                    candidates["complete_address"].append(
                        build_evidence(
                            "complete_address",
                            addr.strip(),
                            p.final_url,
                            pages_by_url,
                            excerpt=addr.strip(),
                            official_domain=official_domain,
                        )
                    )

                tel = obj.get("telephone")
                if isinstance(tel, str) and PHONE_RE.match(tel.strip()):
                    candidates["official_phone_number"].append(
                        build_evidence(
                            "official_phone_number",
                            tel.strip(),
                            p.final_url,
                            pages_by_url,
                            excerpt=tel.strip(),
                            official_domain=official_domain,
                        )
                    )

                em = obj.get("email")
                if isinstance(em, str) and EMAIL_RE.match(em.strip().removeprefix("mailto:")):
                    clean_em = em.strip().removeprefix("mailto:")
                    candidates["official_email_address"].append(
                        build_evidence(
                            "official_email_address",
                            clean_em,
                            p.final_url,
                            pages_by_url,
                            excerpt=clean_em,
                            official_domain=official_domain,
                        )
                    )

                same_as = obj.get("sameAs")
                if isinstance(same_as, str):
                    same_as = [same_as]
                if isinstance(same_as, list):
                    for s_url in same_as:
                        if isinstance(s_url, str) and "linkedin.com/company" in s_url.lower():
                            candidates["linkedin_company_page"].append(
                                build_evidence(
                                    "linkedin_company_page",
                                    s_url.strip(),
                                    p.final_url,
                                    pages_by_url,
                                    excerpt=s_url.strip(),
                                    official_domain=official_domain,
                                )
                            )

        # Pick corporate generic email on official domain if none from JSON-LD.
        if official_domain and p.domain.removeprefix("www.") == official_domain:
            for em in p.emails_found:
                if em.lower().endswith("@" + official_domain) and any(
                    em.lower().startswith(pfx) for pfx in ("info@", "contact@", "sales@", "hello@", "support@", "office@")
                ):
                    candidates["official_email_address"].append(
                        build_evidence(
                            "official_email_address",
                            em,
                            p.final_url,
                            pages_by_url,
                            excerpt=em,
                            official_domain=official_domain,
                        )
                    )
                    break

            for ph in p.phones_found:
                if PHONE_RE.match(ph):
                    candidates["official_phone_number"].append(
                        build_evidence(
                            "official_phone_number",
                            ph,
                            p.final_url,
                            pages_by_url,
                            excerpt=ph,
                            official_domain=official_domain,
                        )
                    )
                    break

        for li in p.linkedin_urls:
            if "linkedin.com/company" in li.lower():
                candidates["linkedin_company_page"].append(
                    build_evidence(
                        "linkedin_company_page",
                        li.split("?", 1)[0],
                        p.final_url,
                        pages_by_url,
                        excerpt=li,
                        official_domain=official_domain,
                    )
                )

    if country_hint and not candidates["country_head_office"] and pages:
        # Only adopt country_hint if pages confirm that country name.
        for p in pages:
            if country_hint.casefold() in p.text.casefold():
                candidates["country_head_office"].append(
                    build_evidence(
                        "country_head_office",
                        country_hint[:100],
                        p.final_url,
                        pages_by_url,
                        excerpt=country_hint,
                        official_domain=official_domain,
                    )
                )
                break

    if not candidates["company_name"] and pages:
        candidates["company_name"].append(
            build_evidence(
                "company_name",
                company_name[:255],
                pages[0].final_url,
                pages_by_url,
                excerpt=pages[0].title or company_name,
                official_domain=official_domain,
            )
        )

    return candidates


async def _lookup_company_market_events(
    company_name: str,
    pages: list[FetchedPage],
    api_client: Any,
) -> tuple[list[str], list[dict[str, Any]], list[FieldEvidence]]:
    """Detect event associations in fetched pages and match ONLY against real Market Intel Event UUIDs."""
    event_hint_re = re.compile(
        r"\b([A-Z][A-Za-z0-9&\- ]{1,60}?(?:Expo|Exhibition|Summit|Conference|Forum|Trade Show|Fair|Conclave|ADIPEC|GITEX|CES)\s*(?:20\d{2})?)\b"
    )
    pages_by_url = {p.final_url: p for p in pages}
    discovered_names: list[tuple[str, str]] = []
    seen: set[str] = set()
    for p in pages:
        for m in event_hint_re.finditer(p.text):
            title = " ".join(m.group(1).split())
            if len(title) >= 5 and title.casefold() not in seen:
                seen.add(title.casefold())
                discovered_names.append((title, p.final_url))
            if len(discovered_names) >= 5:
                break

    matched_ids: list[str] = []
    missing_events: list[dict[str, Any]] = []
    evidences: list[FieldEvidence] = []

    for title, src_url in discovered_names:
        try:
            resp = await api_client.list_market_events({"search": title})
            recs = resp.get("results", []) if isinstance(resp, dict) else (resp if isinstance(resp, list) else [])
            recs = [r for r in recs if isinstance(r, dict)]
        except ApiError:
            recs = []

        # Also check if any visible event_title appears directly in the extracted text when search phrase includes prefix words.
        exact = [
            r for r in recs
            if (
                " ".join(str(r.get("event_title") or "").split()).casefold() == title.casefold()
                or (
                    len(str(r.get("event_title") or "").strip()) >= 5
                    and str(r.get("event_title") or "").strip().casefold() in title.casefold()
                )
            )
            and r.get("id")
        ]
        if exact:
            eid = str(exact[0]["id"])
            if eid not in matched_ids:
                matched_ids.append(eid)
                evidences.append(
                    build_evidence(
                        "market_event_ids",
                        eid,
                        src_url,
                        pages_by_url,
                        excerpt=str(exact[0].get("event_title") or title),
                    )
                )
        else:
            missing_events.append({
                "event_title": title,
                "source_url": src_url,
                "reason": "Event mentioned in company sources is not yet in Market Intel; verify and create event before linking.",
            })

    return matched_ids, missing_events, evidences


async def extract_all_sections(
    company_name: str,
    *,
    company_key: str,
    country_hint: str | None,
    official_website: str | None,
    official_domain: str | None,
    pages: list[FetchedPage],
    requested_sections: set[str],
    api_client: Any,
    router: ProviderRouter | None,
    cache: ResearchCache | None,
    settings: Settings,
    budget: ResearchBudget,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Run prioritized, field-cached extraction across all six prospect sections."""
    pages_by_url = {p.final_url: p for p in pages}
    for p in pages:
        pages_by_url.setdefault(p.final_url.rstrip("/"), p)
    source_hashes = [p.content_hash for p in pages if p.content_hash]

    fresh_cached: dict[str, dict[str, Any]] = {}
    stale_cached: dict[str, dict[str, Any]] = {}
    if cache is not None and not force_refresh:
        fresh_cached, stale_cached = cache.get_company_fields(company_key)

    def _can_reuse_cached_field(field_name: str) -> bool:
        entry = fresh_cached.get(field_name)
        if not entry:
            return False
        if entry.get("verification_status") != VerificationStatus.VERIFIED.value:
            return False
        cached_hashes = set(entry.get("source_hashes") or [])
        if source_hashes and cached_hashes and not cached_hashes.issubset(set(source_hashes)):
            # Underlying page content changed; invalidate cached field so it re-extracts!
            return False
        budget.record_field_cache_hit()
        return True

    def _save_field_cache(field_name: str, value: Any, evs: list[FieldEvidence], status: VerificationStatus) -> None:
        if cache is None or value in (None, "", []):
            return
        ttl = (
            settings.research_volatile_field_ttl_seconds
            if field_name in VOLATILE_FIELDS
            else settings.research_field_ttl_seconds
        )
        cache.set_company_field(company_key, field_name, value, evs, status, source_hashes, ttl)

    all_evidence: dict[str, list[FieldEvidence]] = {}
    conflicts: list[ConflictRecord] = []
    warnings: list[str] = []
    missing_prerequisites: dict[str, list[dict[str, Any]]] = {
        "parent_companies": [],
        "acquiring_companies": [],
        "merging_companies": [],
        "government_entities": [],
        "shareholder_companies": [],
        "market_events": [],
    }
    prospect_fields: dict[str, Any] = {}
    section_outputs: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # PRIORITY 1: Market Offerings & Primary Industries
    # ------------------------------------------------------------------
    business_summary = ""
    if "market_offerings" in requested_sections or "general_information" in requested_sections:
        if (
            _can_reuse_cached_field("primary_industries")
            and _can_reuse_cached_field("offerings_data")
            and _can_reuse_cached_field("primary_offering_type")
        ):
            prospect_fields["primary_industries"] = fresh_cached["primary_industries"]["value"]
            prospect_fields["offerings_data"] = fresh_cached["offerings_data"]["value"]
            prospect_fields["primary_offering_type"] = fresh_cached["primary_offering_type"]["value"]
            for fn in ("primary_industries", "offerings_data", "primary_offering_type"):
                all_evidence[fn] = [FieldEvidence.model_validate(e) for e in fresh_cached[fn]["evidence"]]
            section_outputs["market_offerings"] = {
                "primary_industries": prospect_fields["primary_industries"],
                "primary_offering_type": prospect_fields["primary_offering_type"],
                "offerings_data": prospect_fields["offerings_data"],
                "from_cache": True,
            }
        else:
            off_res = await extract_industries_and_offerings(
                company_name,
                pages,
                official_domain=official_domain,
                router=router,
                budget=budget,
                max_context_chars=settings.research_max_context_chars,
                force_refresh=force_refresh,
            )
            business_summary = off_res.get("business_summary") or ""
            for fn in ("primary_industries", "primary_offering_type", "offerings_data"):
                val = off_res.get(fn)
                if val not in (None, "", []):
                    prospect_fields[fn] = val
                evs = off_res.get("evidence", {}).get(fn, [])
                if evs:
                    all_evidence[fn] = evs
                    _save_field_cache(fn, val, evs, evs[0].verification_status)
            section_outputs["market_offerings"] = {
                "primary_industries": off_res.get("primary_industries"),
                "primary_offering_type": off_res.get("primary_offering_type"),
                "offerings_data": off_res.get("offerings_data", []),
                "offerings_status": off_res.get("offerings_status"),
                "industries_status": off_res.get("industries_status"),
                "business_summary": business_summary,
                "from_cache": False,
            }

    # ------------------------------------------------------------------
    # PRIORITY 2: General Information
    # ------------------------------------------------------------------
    if "general_information" in requested_sections:
        gen_cands = _extract_deterministic_general_info(
            company_name, country_hint, official_website, official_domain, pages
        )
        # Use Stage 2 LLM ONLY for general fields still missing after deterministic pass.
        needed_gen = [
            f for f in ("company_name", "country_head_office", "complete_address", "official_phone_number", "official_email_address", "linkedin_company_page")
            if not gen_cands.get(f) and not _can_reuse_cached_field(f)
        ]
        if needed_gen and router is not None and pages:
            sources_block = format_sources_for_prompt(pages[:4], max_total_chars=min(settings.research_max_context_chars, 8000))
            sys_p, usr_p = general_info_prompt(company_name, sources_block)
            llm_gen = await router.complete_json(
                task_type="general_information",
                system_prompt=sys_p,
                user_prompt=usr_p,
                stage="primary",
                budget=budget,
                max_tokens=700,
                force_refresh=force_refresh,
            )
            if isinstance(llm_gen, dict):
                field_map = {
                    "official_company_name": "company_name",
                    "country_head_office": "country_head_office",
                    "complete_address": "complete_address",
                    "official_phone_number": "official_phone_number",
                    "official_email_address": "official_email_address",
                    "linkedin_company_page": "linkedin_company_page",
                }
                for llm_k, schema_k in field_map.items():
                    node = llm_gen.get(llm_k)
                    if not isinstance(node, dict) or not node.get("value"):
                        continue
                    val = str(node["value"]).strip()
                    if not val or val.lower() == "null":
                        continue
                    if schema_k == "official_phone_number" and not PHONE_RE.match(val):
                        continue
                    if schema_k == "official_email_address" and not EMAIL_RE.match(val):
                        continue
                    if schema_k == "linkedin_company_page" and "linkedin.com" not in val.lower():
                        continue
                    quote = str(node.get("evidence_quote") or val)
                    src_u = str(node.get("source_url") or (pages[0].final_url if pages else ""))
                    if pages and not (is_quote_grounded(val, pages) or is_quote_grounded(quote, pages)):
                        continue
                    gen_cands.setdefault(schema_k, []).append(
                        build_evidence(
                            schema_k,
                            val,
                            src_u,
                            pages_by_url,
                            excerpt=quote,
                            official_domain=official_domain,
                        )
                    )

        gen_section: dict[str, Any] = {}
        for fn, cand_list in gen_cands.items():
            if _can_reuse_cached_field(fn):
                prospect_fields[fn] = fresh_cached[fn]["value"]
                all_evidence[fn] = [FieldEvidence.model_validate(e) for e in fresh_cached[fn]["evidence"]]
                gen_section[fn] = prospect_fields[fn]
                continue

            existing_val = fresh_cached[fn]["value"] if fn in fresh_cached else None
            val, status, evs, conflict = resolve_field_candidates(
                fn, cand_list, existing_verified_value=existing_val
            )
            if conflict is not None:
                # Stage 3 escalation ONLY on unresolved conflicts!
                if conflict.resolution_status == "UNRESOLVED_CONFLICT" and router is not None and pages:
                    csum = "\n".join(f"- {c['value']} ({c['source_type']}: {c['source_url']})" for c in conflict.candidates)
                    s_block = format_sources_for_prompt(pages[:3], max_total_chars=6000)
                    sys_p, usr_p = conflict_resolution_prompt(company_name, fn, csum, s_block)
                    esc = await router.complete_json(
                        task_type=f"conflict_{fn}",
                        system_prompt=sys_p,
                        user_prompt=usr_p,
                        stage="escalation",
                        budget=budget,
                        max_tokens=450,
                        force_refresh=force_refresh,
                    )
                    if isinstance(esc, dict) and esc.get("resolution_status") in ("RESOLVED_BY_AUTHORITY", "RESOLVED_BY_RECENCY") and esc.get("chosen_value") is not None:
                        conflict.resolution_status = esc["resolution_status"]
                        conflict.chosen_value = esc["chosen_value"]
                        conflict.rationale = str(esc.get("rationale") or conflict.rationale)
                        val = esc["chosen_value"]
                        status = VerificationStatus.PARTIALLY_VERIFIED
                conflicts.append(conflict)

            if fn == "official_email_address" and val:
                verified_co_email, co_status, co_reason = await verify_company_email(str(val), pages, api_client)
                if verified_co_email:
                    prospect_fields[fn] = verified_co_email
                    gen_section[fn] = verified_co_email
                    _save_field_cache(fn, verified_co_email, evs, status)
                else:
                    warnings.append(f"Company email '{val}' excluded from payload (instant-verify={co_status}: {co_reason}).")
                    gen_section[fn] = None
                    gen_section["official_email_verification"] = {"candidate": val, "status": co_status, "reason": co_reason}
            elif val not in (None, ""):
                prospect_fields[fn] = val
                gen_section[fn] = val
                _save_field_cache(fn, val, evs, status)
            if evs:
                all_evidence[fn] = evs
        if "primary_industries" in prospect_fields:
            gen_section["primary_industries"] = prospect_fields["primary_industries"]
        section_outputs["general_information"] = gen_section

    # ------------------------------------------------------------------
    # PRIORITY 3 & 4: Corporate Structure & Ownership
    # ------------------------------------------------------------------
    corp_llm: dict[str, Any] | None = None
    need_corp_or_own = bool(requested_sections & {"corporate_structure", "shareholding_ownership"})
    if need_corp_or_own and router is not None and pages:
        corp_pages = sorted(
            pages,
            key=lambda p: sum(1 for k in ("investor", "shareholder", "governance", "annual", "about", "group", "subsidiar", "legal", "sec", "registry") if k in p.final_url.lower()),
            reverse=True,
        )[:4]
        sources_block = format_sources_for_prompt(corp_pages, max_total_chars=min(settings.research_max_context_chars, 9000))
        sys_p, usr_p = corporate_structure_prompt(company_name, sources_block)
        corp_llm = await router.complete_json(
            task_type="corporate_structure_and_ownership",
            system_prompt=sys_p,
            user_prompt=usr_p,
            stage="primary",
            budget=budget,
            max_tokens=1100,
            force_refresh=force_refresh,
        )
        # Escalate to Stage 3 model if explicit ambiguity or conflicting structure notes are flagged.
        if isinstance(corp_llm, dict) and corp_llm.get("ambiguity_notes"):
            esc_sys, esc_usr = corporate_structure_prompt(company_name, sources_block)
            esc_res = await router.complete_json(
                task_type="corporate_structure_escalation",
                system_prompt=esc_sys,
                user_prompt=esc_usr,
                stage="escalation",
                budget=budget,
                max_tokens=1100,
                force_refresh=force_refresh,
            )
            if isinstance(esc_res, dict):
                corp_llm = esc_res

    if "corporate_structure" in requested_sections:
        corp_res = await resolve_corporate_structure(
            company_name,
            corp_llm,
            pages,
            official_domain=official_domain,
            api_client=api_client,
        )
        for fn in (
            "company_structure",
            "operational_status",
            "ownership_sector",
            "parent_companies",
            "acquiring_companies_data",
            "merging_companies",
            "dissolved_companies",
        ):
            val = corp_res.get(fn)
            if val not in (None, "", []):
                prospect_fields[fn] = val
        all_evidence.update(corp_res.get("evidence", {}))
        for k in ("parent_companies", "acquiring_companies", "merging_companies"):
            missing_prerequisites[k].extend(corp_res["missing_prerequisites"].get(k, []))
        section_outputs["corporate_structure"] = {
            "company_structure": corp_res.get("company_structure"),
            "operational_status": corp_res.get("operational_status"),
            "ownership_sector": corp_res.get("ownership_sector"),
            "parent_companies": corp_res.get("parent_companies", []),
            "verified_parent_details": corp_res.get("verified_parent_details", []),
            "subsidiaries_and_branches": corp_res.get("subsidiaries_and_branches", []),
            "acquiring_companies_data": corp_res.get("acquiring_companies_data", []),
            "merging_companies": corp_res.get("merging_companies", []),
            "dissolved_companies": corp_res.get("dissolved_companies", []),
            "ambiguity_notes": corp_res.get("ambiguity_notes", []),
        }

    # ------------------------------------------------------------------
    # PRIORITY 6: Key Contacts (run before ownership so Individual contact_index can align)
    # ------------------------------------------------------------------
    contact_candidates = []
    if "key_contacts" in requested_sections:
        c_res = await research_key_contacts(
            company_name,
            pages,
            official_domain=official_domain,
            api_client=api_client,
            router=router,
            budget=budget,
            max_context_chars=settings.research_max_context_chars,
            force_refresh=force_refresh,
        )
        contact_candidates = c_res.get("all_candidates", [])
        if c_res.get("key_contacts"):
            prospect_fields["key_contacts"] = c_res["key_contacts"]
        all_evidence.update(c_res.get("evidence", {}))
        section_outputs["key_contacts"] = {
            "verified_contacts": c_res.get("key_contacts", []),
            "excluded_candidates_count": c_res.get("excluded_count", 0),
        }

    # ------------------------------------------------------------------
    # PRIORITY 4: Shareholding / Ownership
    # ------------------------------------------------------------------
    if "shareholding_ownership" in requested_sections:
        own_res = await resolve_ownership(
            company_name,
            prospect_fields.get("country_head_office") or country_hint,
            corp_llm,
            pages,
            official_domain=official_domain,
            api_client=api_client,
            verified_contacts=prospect_fields.get("key_contacts") or [],
        )
        if own_res.get("shareholdings"):
            prospect_fields["shareholdings"] = own_res["shareholdings"]
        all_evidence.update(own_res.get("evidence", {}))
        missing_prerequisites["government_entities"].extend(own_res["missing_prerequisites"].get("government_entities", []))
        missing_prerequisites["shareholder_companies"].extend(own_res["missing_prerequisites"].get("shareholder_companies", []))
        section_outputs["shareholding_ownership"] = {
            "shareholdings": own_res.get("shareholdings", []),
            "incomplete_shareholders": own_res.get("incomplete_shareholders", []),
            "directors_identified_not_shareholders": own_res.get("directors_identified_not_shareholders", []),
            "ownership_status": own_res.get("ownership_status"),
            "total_verified_percentage": own_res.get("total_verified_percentage", 0.0),
        }

    # ------------------------------------------------------------------
    # PRIORITY 5: Market Events
    # ------------------------------------------------------------------
    if "market_events" in requested_sections:
        ev_ids, missing_evs, ev_evidences = await _lookup_company_market_events(company_name, pages, api_client)
        if ev_ids:
            prospect_fields["market_event_ids"] = ev_ids
        if ev_evidences:
            all_evidence["market_event_ids"] = ev_evidences
        missing_prerequisites["market_events"].extend(missing_evs)
        section_outputs["market_events"] = {
            "market_event_ids": ev_ids,
            "unlinked_events_discovered": missing_evs,
        }

    return {
        "prospect_fields": prospect_fields,
        "sections": section_outputs,
        "evidence": all_evidence,
        "conflicts": conflicts,
        "missing_prerequisites": missing_prerequisites,
        "contact_candidates": contact_candidates,
        "business_summary": business_summary,
        "warnings": warnings,
    }
