import uuid

import pytest

from schemas import parse_prospect
from validation import business_rule_errors
from conftest import PARENT_ID, valid_prospect


def errs(raw):
    p, e = parse_prospect(raw)
    if p is not None:
        e = business_rule_errors(p)
    return p, " | ".join(e)


def test_valid_payload_parses_and_normalises():
    p, e = parse_prospect(valid_prospect(company_name="  ABC Technologies  "))
    assert e == [] and p.company_name == "ABC Technologies"
    assert p.to_api_payload()["country_head_office"] == "India"


@pytest.mark.parametrize("field", ["company_name", "country_head_office", "complete_address",
                                   "primary_industries", "ownership_sector", "company_structure",
                                   "operational_status", "primary_offering_type"])
def test_missing_required_field(field):
    raw = valid_prospect(); raw.pop(field)
    p, e = parse_prospect(raw)
    assert p is None and any(x.startswith(field) and "required" in x for x in e)


def test_blank_required_field_rejected():
    assert parse_prospect(valid_prospect(complete_address="   "))[0] is None


@pytest.mark.parametrize("field,value", [
    ("company_structure", "Joint Venture"), ("operational_status", "Dormant"),
    ("ownership_sector", "NGO"), ("primary_offering_type", "Hardware"),
])
def test_invalid_enum(field, value):
    p, e = parse_prospect(valid_prospect(**{field: value}))
    assert p is None and field in e[0]


def test_invalid_offering_type():
    assert parse_prospect(valid_prospect(offerings_data=[{"offering_type": "Hardware", "name": "x"}]))[0] is None


def test_invalid_email_syntax():
    assert parse_prospect(valid_prospect(official_email_address="not-an-email"))[0] is None


def test_optional_email_omitted_ok():
    raw = valid_prospect(); raw.pop("official_email_address")
    assert parse_prospect(raw)[1] == []


def test_invalid_website_and_linkedin():
    assert parse_prospect(valid_prospect(official_website_url="abc"))[0] is None
    assert parse_prospect(valid_prospect(linkedin_company_page="https://example.com/x"))[0] is None


def test_unsupported_fields_rejected_not_dropped():
    p, e = parse_prospect(valid_prospect(email="x@y.com"))
    assert p is None and "not a supported field" in e[0]


def test_branch_and_subsidiary_need_parent():
    for s in ("Branch", "Subsidiary"):
        _, e = errs(valid_prospect(company_structure=s))
        assert "parent" in e.lower()


def test_parent_with_parent_rejected_and_duplicate_ids():
    _, e = errs(valid_prospect(company_structure="Branch", parent_companies=[PARENT_ID, PARENT_ID]))
    assert "repeated" in e


def test_invalid_parent_uuid():
    assert parse_prospect(valid_prospect(company_structure="Branch", parent_companies=["nope"]))[0] is None


def test_duplicate_offerings():
    o = {"offering_type": "Service", "name": "Consulting"}
    _, e = errs(valid_prospect(offerings_data=[o, {**o, "name": "consulting"}]))
    assert "duplicate offering" in e


def test_offering_must_match_primary_type():
    _, e = errs(valid_prospect(offerings_data=[{"offering_type": "Product", "name": "Pump"}]))
    assert "does not match primary_offering_type" in e


def test_multiple_allows_mixed_offerings():
    _, e = errs(valid_prospect(primary_offering_type="Multiple", offerings_data=[
        {"offering_type": "Product", "name": "Pump"}, {"offering_type": "Service", "name": "Repair"}]))
    assert e == ""


@pytest.mark.parametrize("bad", [
    {"contact_name": "", "designation": "x"},
    {"contact_name": "A", "designation": "x", "phone_number": "abc"},
    {"contact_name": "A", "designation": "x", "official_email": "bad"},
    {"contact_name": "A", "designation": "x", "linkedin_profile": "https://evil.com/a"},
    {"contact_name": "A", "designation": "x", "role": "ceo"},
])
def test_invalid_key_contacts(bad):
    assert parse_prospect(valid_prospect(key_contacts=[bad]))[0] is None


@pytest.mark.parametrize("pct", [0, -5, 100.5])
def test_invalid_share_percentage(pct):
    sh = [{"holder_type": "Other", "other_name": "Fund", "share_percentage": pct}]
    assert parse_prospect(valid_prospect(shareholdings=sh))[0] is None


def test_shareholding_total_over_100():
    sh = [{"holder_type": "Other", "other_name": "A", "share_percentage": 60},
          {"holder_type": "Other", "other_name": "B", "share_percentage": 50}]
    assert "exceeds 100" in errs(valid_prospect(shareholdings=sh))[1]


@pytest.mark.parametrize("sh", [
    {"holder_type": "Company", "share_percentage": 10},
    {"holder_type": "Individual", "share_percentage": 10},
    {"holder_type": "Government", "share_percentage": 10},
    {"holder_type": "Other", "share_percentage": 10},
])
def test_invalid_shareholding_relationships(sh):
    assert parse_prospect(valid_prospect(shareholdings=[sh]))[0] is None


def test_individual_shareholder_index_out_of_range():
    sh = [{"holder_type": "Individual", "contact_index": 5, "share_percentage": 10}]
    assert "contact_index" in errs(valid_prospect(shareholdings=sh))[1]


def test_government_shareholder_and_market_events_accepted_when_valid():
    gov_id = str(uuid.uuid4())
    event_id = str(uuid.uuid4())
    sh = [{"holder_type": "Government", "government_entity": gov_id, "share_percentage": 10}]
    p, e = parse_prospect(valid_prospect(shareholdings=sh, market_event_ids=[event_id]))
    assert e == []
    assert p.market_event_ids == [uuid.UUID(event_id)]
    assert business_rule_errors(p) == []


def test_government_sector_requires_government_shareholder():
    p, _ = parse_prospect(valid_prospect(ownership_sector="Government Company"))
    errs_list = business_rule_errors(p)
    assert any("Government Company" in err and "government entity" in err.lower() for err in errs_list)


def test_invalid_market_event_ids_rejected():
    p, e = parse_prospect(valid_prospect(market_event_ids=["not-a-uuid"]))
    assert p is None and any("market_event_ids" in err for err in e)


def test_government_entity_schema_validation():
    from schemas import parse_government_entity
    valid = {"name": "Ministry of Energy", "country": "India", "is_state_government": False}
    g, errs = parse_government_entity(valid)
    assert errs == [] and g.name == "Ministry of Energy"

    # Extra fields rejected
    bad = {**valid, "unsupported_field": "bad"}
    g_bad, errs_bad = parse_government_entity(bad)
    assert g_bad is None and any("not a supported field" in e for e in errs_bad)

    # Missing required name
    bad2 = {"country": "India"}
    g_bad2, errs_bad2 = parse_government_entity(bad2)
    assert g_bad2 is None and any("name" in e for e in errs_bad2)


def test_market_event_schema_validation():
    from schemas import parse_market_event
    valid = {
        "event_title": "Energy Summit 2026",
        "host_country": "India",
        "start_date": "2026-10-01",
        "end_date": "2026-10-03",
        "website_url": "https://energysummit.example.com",
    }
    m, errs = parse_market_event(valid)
    assert errs == [] and m.event_title == "Energy Summit 2026"

    # End date before start date rejected
    bad_dates = {**valid, "start_date": "2026-10-05", "end_date": "2026-10-02"}
    m_bad, errs_bad = parse_market_event(bad_dates)
    assert m_bad is None and any("end_date cannot be earlier than start_date" in e for e in errs_bad)


def test_prospect_blueprint_structure():
    from schemas import PROSPECT_BLUEPRINT
    sections = PROSPECT_BLUEPRINT["sections"]
    section_names = {s["title"] for s in sections}
    assert "Corporate Structure & Parent Auto-Linking" in section_names
    assert "General Information" in section_names
    assert "Shareholding / Ownership" in section_names
    assert "Market Offerings" in section_names
    assert "Market Events" in section_names
    assert "Key Contacts / Personnel" in section_names
    assert "Government Entity Association" in section_names


def test_acquired_and_merged_rules():
    assert "acquiring_companies_data" in errs(valid_prospect(operational_status="Acquired"))[1]
    assert "merging_companies" in errs(valid_prospect(operational_status="Merged"))[1]
    assert "only allowed" in errs(valid_prospect(merging_companies=[PARENT_ID]))[1]
    ok = valid_prospect(operational_status="Acquired", acquiring_companies_data=[{"acquiring_company": PARENT_ID}])
    assert errs(ok)[1] == ""
    bad = valid_prospect(operational_status="Merged", merging_companies=[PARENT_ID], dissolved_companies=[PARENT_ID])
    assert "both merging and dissolved" in errs(bad)[1]
