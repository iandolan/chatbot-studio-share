"""Preset library: listing, instantiation, and that the Law Firm Intake
Receptionist ships real guardrails reflected in the assembled prompt."""
import uuid

from server import chat, db, presets


def _bot_row(app, bot_id):
    with app.app_context():
        return db.query("SELECT * FROM bots WHERE id = ?", (bot_id,), one=True)


def test_list_presets_endpoint(client):
    resp = client.get("/api/presets")
    assert resp.status_code == 200
    slugs = {p["slug"] for p in resp.get_json()["presets"]}
    assert {"law-firm-intake", "local-service-lead", "internal-kb"} <= slugs
    legal = next(p for p in resp.get_json()["presets"] if p["slug"] == "law-firm-intake")
    assert legal["guardrails"]                # non-empty refusal list
    assert legal["guardrail_note"]            # dashboard "why it matters" note


def test_instantiate_preset_creates_working_bot(client):
    resp = client.post("/api/bots", json={"preset": "law-firm-intake"})
    assert resp.status_code == 201
    bot = resp.get_json()["bot"]
    # Name defaults to the preset name; provenance recorded.
    assert bot["name"] == "Law Firm Intake Receptionist"
    assert bot["preset"] == "law-firm-intake"
    assert bot["tone"] == "empathetic"
    assert bot["system_prompt"]               # hardened prompt copied in
    assert len(bot["guardrails"]) == len(presets.PRESETS["law-firm-intake"]["config"]["guardrails"])


def test_instantiate_preset_respects_custom_name(client):
    resp = client.post("/api/bots", json={"preset": "law-firm-intake", "name": "Dolan & Co"})
    assert resp.get_json()["bot"]["name"] == "Dolan & Co"


def test_unknown_preset_rejected(client):
    resp = client.post("/api/bots", json={"preset": "does-not-exist"})
    assert resp.status_code == 400


def test_plain_bot_creation_still_works(client):
    # Backward compatibility: no preset key = old behavior.
    resp = client.post("/api/bots", json={"name": f"Plain {uuid.uuid4().hex[:5]}"})
    assert resp.status_code == 201
    bot = resp.get_json()["bot"]
    assert bot["preset"] == ""
    assert bot["mode"] == "public"
    assert bot["guardrails"] == []


def test_legal_lead_fields_capture_intake_data(client):
    bot = client.post("/api/bots", json={"preset": "law-firm-intake"}).get_json()["bot"]
    keys = {f["key"] for f in bot["lead_fields"]}
    # name/contact, matter type, urgency, and the conflict-check opposing party.
    assert {"name", "phone", "matter_type", "opposing_party", "urgency"} <= keys
    name_field = next(f for f in bot["lead_fields"] if f["key"] == "name")
    assert name_field["required"] is True


def test_legal_guardrails_present_in_system_prompt(app, client):
    bot_id = client.post("/api/bots", json={"preset": "law-firm-intake"}).get_json()["bot"]["id"]
    row = _bot_row(app, bot_id)
    prompt = chat.build_system_prompt(row, [], [], lead_submitted=False, convo_status="bot")
    lower = prompt.lower()
    assert "owner's guardrails" in lower
    # The specific bar/UPL protections must be reflected in the live prompt.
    assert "legal advice" in lower
    assert "attorney will follow up" in lower
    assert "conflict" in lower
    assert "represent" in lower


def test_guardrails_reasserted_every_turn_via_config(app, client):
    """Guardrails are config, not baked prose: clearing them drops the block,
    proving they travel with the bot rather than being frozen into the prompt."""
    bot_id = client.post("/api/bots", json={"preset": "law-firm-intake"}).get_json()["bot"]["id"]
    client.patch(f"/api/bots/{bot_id}", json={"guardrails": []})
    row = _bot_row(app, bot_id)
    prompt = chat.build_system_prompt(row, [], [], lead_submitted=False, convo_status="bot")
    assert "hard guardrails" not in prompt.lower()


def test_local_service_preset_instantiates(client):
    bot = client.post("/api/bots", json={"preset": "local-service-lead"}).get_json()["bot"]
    assert bot["mode"] == "public"
    assert {"name", "phone"} <= {f["key"] for f in bot["lead_fields"]}
    assert bot["guardrails"]


def test_legal_preset_chats_in_mock_mode(client):
    bot = client.post("/api/bots", json={"preset": "law-firm-intake"}).get_json()["bot"]
    resp = client.post("/api/widget/chat", json={
        "key": bot["embed_key"], "message": "I was in a car accident",
        "session_id": uuid.uuid4().hex[:16], "page_url": "https://firm.example/",
    })
    assert resp.status_code == 200
    assert resp.get_json()["reply"]
