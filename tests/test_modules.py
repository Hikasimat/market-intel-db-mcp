import copy
import uuid
import pytest

from api_client import ApiError, EndpointNotAllowed
from confirmation_store import ConfirmationError
from conftest import valid_prospect, valid_government_entity, valid_market_event


# ==============================================================================
# 1. Prospect Blueprint Discovery
# ==============================================================================
async def test_get_prospect_blueprint(wf):
    res = await wf.get_blueprint()
    assert res["status"] == "success"
    blueprint = res["blueprint"]
    assert "sections" in blueprint
    assert "lookup_modules" in blueprint
    assert "government_entity" in blueprint["lookup_modules"]
    assert "market_event" in blueprint["lookup_modules"]

    # Check that required sections exist
    section_titles = [s["title"] for s in blueprint["sections"]]
    assert "Corporate Structure & Parent Auto-Linking" in section_titles
    assert "General Information" in section_titles
    assert "Shareholding / Ownership" in section_titles
    assert "Market Offerings" in section_titles
    assert "Market Events" in section_titles
    assert "Key Contacts / Personnel" in section_titles
    assert "Government Entity Association" in section_titles


# ==============================================================================
# 2. Government Entities Module
# ==============================================================================
async def test_government_entity_search_and_reuse(wf, client):
    existing_id = str(uuid.uuid4())
    client.visible_gov_entities = [
        {"id": existing_id, "name": "Department of Energy", "country": "India"}
    ]
    search_res = await wf.search_gov_entities("Energy")
    assert search_res["ok"]
    assert search_res["count"] == 1
    assert search_res["matches"][0]["id"] == existing_id


async def test_government_entity_create_workflow(wf, client):
    payload = valid_government_entity(name="Ministry of New & Renewable Energy")
    val_res = await wf.validate_government_entity(payload)
    assert val_res["valid"]
    assert "confirmation_token" in val_res
    token = val_res["confirmation_token"]

    # Creating without explicit confirmation rejected
    create_refused = await wf.create_government_entity(token, user_has_explicitly_confirmed=False)
    assert not create_refused["created"]
    assert "Explicit user confirmation" in create_refused["errors"][0]

    # Creating with explicit confirmation succeeds
    create_res = await wf.create_government_entity(token, user_has_explicitly_confirmed=True)
    assert create_res["created"]
    new_id = create_res["id"]
    assert new_id
    assert client.count("create_gov") == 1

    # Verify record was stored in client
    fetched = await wf.get_record("government_entity", new_id)
    assert fetched["ok"]
    assert fetched["record"]["name"] == "Ministry of New & Renewable Energy"


async def test_government_entity_duplicate_detection(wf, client):
    existing_id = str(uuid.uuid4())
    client.visible_gov_entities = [
        {"id": existing_id, "name": "Department of Energy", "country": "India"}
    ]
    # Exact duplicate blocks validation
    exact = valid_government_entity(name="department of energy", country="India")
    val_exact = await wf.validate_government_entity(exact)
    assert not val_exact["valid"]
    assert any("Duplicate" in e for e in val_exact["errors"])

    # Similar duplicate produces warning
    similar = valid_government_entity(name="Department of Energy & Power", country="India")
    val_sim = await wf.validate_government_entity(similar)
    assert val_sim["valid"]
    assert any("Possible duplicate" in w for w in val_sim["warnings"])


# ==============================================================================
# 3. Market Events Module
# ==============================================================================
async def test_market_event_search_and_reuse(wf, client):
    existing_id = str(uuid.uuid4())
    client.visible_market_events = [
        {"id": existing_id, "event_title": "Global Tech Summit 2026", "host_country": "India"}
    ]
    search_res = await wf.search_events("Summit")
    assert search_res["ok"]
    assert search_res["count"] == 1
    assert search_res["matches"][0]["id"] == existing_id


async def test_market_event_create_workflow(wf, client):
    payload = valid_market_event(event_title="Renewable Energy Expo 2026")
    val_res = await wf.validate_market_event(payload)
    assert val_res["valid"]
    token = val_res["confirmation_token"]

    create_res = await wf.create_market_event(token, user_has_explicitly_confirmed=True)
    assert create_res["created"]
    new_id = create_res["id"]
    assert new_id
    assert client.count("create_event") == 1

    fetched = await wf.get_record("market_event", new_id)
    assert fetched["ok"]
    assert fetched["record"]["event_title"] == "Renewable Energy Expo 2026"


# ==============================================================================
# 4. Lookup Resolution & Dependency Order in Prospect Creation
# ==============================================================================
async def test_prospect_creation_requires_verified_government_entity(wf, client):
    nonexistent_gov_id = str(uuid.uuid4())
    sh = [{"holder_type": "Government", "government_entity": nonexistent_gov_id, "share_percentage": 25}]
    payload = valid_prospect(shareholdings=sh)

    # Validating with nonexistent government entity fails
    val_res = await wf.validate(payload)
    assert not val_res["valid"]
    assert any("Government Entity" in e and "not found" in e for e in val_res["errors"])

    # Once government entity is created and exists in client:
    gov_rec = {"id": nonexistent_gov_id, "name": "Dept of Space", "country": "India"}
    client.visible_gov_entities.append(gov_rec)

    val_res_ok = await wf.validate(payload)
    assert val_res_ok["valid"]
    assert val_res_ok["confirmation_token"]


async def test_prospect_creation_requires_verified_market_events(wf, client):
    nonexistent_event_id = str(uuid.uuid4())
    payload = valid_prospect(market_event_ids=[nonexistent_event_id])

    # Validating with nonexistent market event fails
    val_res = await wf.validate(payload)
    assert not val_res["valid"]
    assert any("Market Event" in e and "not found" in e for e in val_res["errors"])

    # Once market event exists in client:
    event_rec = {"id": nonexistent_event_id, "event_title": "Tech Expo", "host_country": "India"}
    client.visible_market_events.append(event_rec)

    val_res_ok = await wf.validate(payload)
    assert val_res_ok["valid"]


async def test_full_dependency_creation_flow(wf, client):
    # Step 1: Create missing Government Entity
    gov_payload = valid_government_entity(name="Ministry of Renewable Energy", country="India")
    gov_val = await wf.validate_government_entity(gov_payload)
    assert gov_val["valid"]
    gov_res = await wf.create_government_entity(gov_val["confirmation_token"], True)
    assert gov_res["created"]
    gov_id = gov_res["id"]

    # Step 2: Create missing Market Event
    event_payload = valid_market_event(event_title="Clean Energy Conclave 2026", host_country="India")
    event_val = await wf.validate_market_event(event_payload)
    assert event_val["valid"]
    event_res = await wf.create_market_event(event_val["confirmation_token"], True)
    assert event_res["created"]
    event_id = event_res["id"]

    # Step 3: Validate and Create Prospect referencing both verified records
    sh = [{"holder_type": "Government", "government_entity": gov_id, "share_percentage": 51}]
    prospect_payload = valid_prospect(
        company_name="Green Power Solar Ltd",
        ownership_sector="Government Company",
        shareholdings=sh,
        market_event_ids=[event_id],
    )
    p_val = await wf.validate(prospect_payload)
    assert p_val["valid"]
    p_res = await wf.create(p_val["confirmation_token"], True)
    assert p_res["created"]
    p_id = p_res["id"]
    assert p_id
    assert p_res["company_name"] == "Green Power Solar Ltd"


# ==============================================================================
# 5. Record Update Workflow & Stale Concurrent Modification Detection
# ==============================================================================
async def test_update_record_with_diff_and_confirmation(wf, client):
    # Setup existing government entity
    entity_id = str(uuid.uuid4())
    client.visible_gov_entities.append({
        "id": entity_id,
        "name": "Dept of Technology",
        "country": "India",
        "complete_address": "Old address",
    })

    # Validate update
    val_res = await wf.validate_record_update(
        module="government_entity",
        record_id=entity_id,
        changes={"complete_address": "Updated cutting-edge tech address", "country": "Singapore"}
    )
    assert val_res["valid"]
    assert "diff" in val_res
    diff = val_res["diff"]
    assert diff["complete_address"]["old"] == "Old address"
    assert diff["complete_address"]["new"] == "Updated cutting-edge tech address"
    token = val_res["confirmation_token"]

    # Explicit confirmation required
    refused = await wf.update_confirmed_record(token, user_has_explicitly_confirmed=False)
    assert not refused["updated"]
    assert "Explicit user confirmation" in refused["errors"][0]

    # Confirmed update succeeds
    updated = await wf.update_confirmed_record(token, user_has_explicitly_confirmed=True)
    assert updated["updated"]
    assert updated["outcome"] in ("SUCCESS", "CONFIRMED_SUCCESS")
    assert updated["record"]["complete_address"] == "Updated cutting-edge tech address"
    assert updated["record"]["country"] == "Singapore"


async def test_update_record_stale_detection_blocks_clobbering(wf, client):
    entity_id = str(uuid.uuid4())
    client.visible_gov_entities.append({
        "id": entity_id,
        "name": "Dept of Technology",
        "country": "India",
        "complete_address": "Original address",
    })

    # Staging update
    val_res = await wf.validate_record_update(
        module="government_entity",
        record_id=entity_id,
        changes={"complete_address": "User A change"}
    )
    assert val_res["valid"]
    token = val_res["confirmation_token"]

    # Simulate concurrent modification in backend before User A commits
    client.visible_gov_entities[0]["complete_address"] = "User B concurrent change"

    # Commit attempt by User A should be detected as STALE
    attempt = await wf.update_confirmed_record(token, user_has_explicitly_confirmed=True)
    assert not attempt["updated"]
    assert attempt["outcome"] == "STALE_ABORTED"
    assert any("concurrent" in e.lower() for e in attempt["errors"])


async def test_update_record_disallowed_fields_rejected(wf, client):
    entity_id = str(uuid.uuid4())
    client.visible_gov_entities.append({
        "id": entity_id,
        "name": "Dept of Technology",
        "country": "India"
    })

    # Attempting to change uneditable fields like 'id' or arbitrary fields
    val_res = await wf.validate_record_update(
        module="government_entity",
        record_id=entity_id,
        changes={"id": str(uuid.uuid4()), "fake_field": 123}
    )
    assert not val_res["valid"]
    assert any("not editable" in e for e in val_res["errors"])


# ==============================================================================
# 6. Security & Confirmation Token Inviolability
# ==============================================================================
async def test_confirmation_token_bound_to_entity_and_op_type(wf, client, store):
    gov_payload = valid_government_entity(name="Test Ministry")
    val_gov = await wf.validate_government_entity(gov_payload)
    token = val_gov["confirmation_token"]

    # Attempting to use a Government Entity creation token on Prospect creation fails
    prospect_res = await wf.create(token, user_has_explicitly_confirmed=True)
    assert not prospect_res["created"]
    assert any("mismatch" in e.lower() for e in prospect_res["errors"])

    # Attempting to use another creation token on an update fails
    val_gov2 = await wf.validate_government_entity(valid_government_entity(name="Test Ministry 2"))
    token2 = val_gov2["confirmation_token"]
    update_res = await wf.update_confirmed_record(token2, user_has_explicitly_confirmed=True)
    assert not update_res["updated"]
    assert any("mismatch" in e.lower() for e in update_res["errors"])
