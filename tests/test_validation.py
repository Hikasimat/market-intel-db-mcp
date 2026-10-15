from conftest import PARENT_ID, valid_prospect


async def test_valid_prospect_gets_token_and_nothing_created(wf, client):
    r = await wf.validate(valid_prospect())
    assert r["valid"] and r["confirmation_token"]
    assert client.count("create") == 0
    assert r["payload_to_be_submitted"]["company_name"] == "ABC Technologies"
    assert "Arun Kumar" in r["summary"]


async def test_invalid_data_gets_no_token(wf, client):
    r = await wf.validate(valid_prospect(company_name=""))
    assert not r["valid"] and "confirmation_token" not in r
    assert client.count("verify") == 0 and client.count("create") == 0


async def test_exact_visible_duplicate_blocks(wf, client):
    client.visible = [{"id": "x1", "company_name": "abc technologies", "country_head_office": "India"}]
    r = await wf.validate(valid_prospect())
    assert not r["valid"] and any("Duplicate" in e for e in r["errors"])


async def test_similar_duplicate_is_warning_and_no_match_is_not_proof(wf, client):
    client.visible = [{"id": "x2", "company_name": "ABC Technologies Pvt", "country_head_office": "India"}]
    r = await wf.validate(valid_prospect())
    assert r["valid"]
    assert any("Possible duplicate" in w for w in r["warnings"])
    assert any("NOT proof" in w for w in r["warnings"])


async def test_parent_must_exist(wf, client):
    r = await wf.validate(valid_prospect(company_structure="Branch", parent_companies=[PARENT_ID]))
    assert not r["valid"] and any("not found" in e for e in r["errors"])
    client.visible = [{"id": PARENT_ID, "company_name": "Mother Corp"}]
    r = await wf.validate(valid_prospect(company_structure="Branch", parent_companies=[PARENT_ID]))
    assert r["valid"]


async def test_search_is_minimal_and_limited(wf, client):
    client.visible = [{"id": str(i), "company_name": f"Acme {i}", "country_head_office": "India",
                       "key_contacts": [{"official_email": "secret@x.com"}], "notes": "private"} for i in range(40)]
    r = await wf.search("Acme", limit=1000)
    assert r["count"] == 25
    assert "key_contacts" not in r["matches"][0] and "notes" not in r["matches"][0]
    assert "NOT proof" in r["note"]
    assert (await wf.search("Acme", country="France"))["count"] == 0


async def test_search_rejects_empty_query(wf):
    assert not (await wf.search("  "))["ok"]


async def test_non_pre_rejected(wf, client):
    client.role = "LQ"
    r = await wf.validate(valid_prospect())
    assert not r["valid"] and "PRE" in r["errors"][0]
    r = await wf.search("x")
    assert not r["ok"]
