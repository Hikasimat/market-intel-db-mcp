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


def test_government_shareholder_and_market_events_rejected_as_unverifiable():
    sh = [{"holder_type": "Government", "government_entity": str(uuid.uuid4()), "share_percentage": 10}]
    assert "cannot be verified" in errs(valid_prospect(shareholdings=sh))[1]
    assert "market_event_ids" in errs(valid_prospect(market_event_ids=["1"]))[1]


def test_acquired_and_merged_rules():
    assert "acquiring_companies_data" in errs(valid_prospect(operational_status="Acquired"))[1]
    assert "merging_companies" in errs(valid_prospect(operational_status="Merged"))[1]
    assert "only allowed" in errs(valid_prospect(merging_companies=[PARENT_ID]))[1]
    ok = valid_prospect(operational_status="Acquired", acquiring_companies_data=[{"acquiring_company": PARENT_ID}])
    assert errs(ok)[1] == ""
    bad = valid_prospect(operational_status="Merged", merging_companies=[PARENT_ID], dissolved_companies=[PARENT_ID])
    assert "both merging and dissolved" in errs(bad)[1]
