"""Internal knowledge-base mode: mode flag, passphrase gating, and citation
surfacing from a seeded KB."""
import uuid

from server import db
from server.security import hash_passphrase, verify_passphrase, unlock_token


def _seed_kb(client, bot_id, title, content):
    resp = client.post(f"/api/bots/{bot_id}/sources",
                       json={"type": "text", "title": title, "content": content})
    assert resp.status_code in (201, 207)


def _internal_kb_bot(client, passphrase="hunter2"):
    bot = client.post("/api/bots", json={"preset": "internal-kb"}).get_json()["bot"]
    _seed_kb(client, bot["id"],
             "HR Handbook",
             "Employees accrue 15 vacation days per year. Vacation must be approved "
             "by your manager two weeks in advance.")
    if passphrase:
        r = client.post(f"/api/bots/{bot['id']}/passphrase", json={"passphrase": passphrase})
        assert r.status_code == 200
    return bot


# ---------- passphrase hashing primitives ----------

def test_passphrase_hash_roundtrip():
    stored = hash_passphrase("correct horse")
    assert stored.startswith("pbkdf2$")
    assert "correct horse" not in stored          # never stored in the clear
    assert verify_passphrase("correct horse", stored)
    assert not verify_passphrase("wrong", stored)


def test_unlock_token_is_stable_and_bound_to_key():
    assert unlock_token("pk_aaa") == unlock_token("pk_aaa")
    assert unlock_token("pk_aaa") != unlock_token("pk_bbb")


# ---------- mode flag ----------

def test_internal_kb_preset_sets_mode_and_citations(client):
    bot = client.post("/api/bots", json={"preset": "internal-kb"}).get_json()["bot"]
    assert bot["mode"] == "internal_kb"
    assert bot["cite_sources"] == 1


def test_mode_is_editable_and_backward_compatible(client, bot_id):
    # A plain bot defaults to public and can be switched to internal_kb.
    bot = client.get(f"/api/bots/{bot_id}").get_json()["bot"]
    assert bot["mode"] == "public"
    resp = client.patch(f"/api/bots/{bot_id}", json={"mode": "internal_kb"})
    assert resp.status_code == 200
    assert resp.get_json()["bot"]["mode"] == "internal_kb"


def test_invalid_mode_rejected(client, bot_id):
    resp = client.patch(f"/api/bots/{bot_id}", json={"mode": "banana"})
    assert resp.status_code == 400


# ---------- gated access ----------

def test_config_reports_gated(client):
    bot = _internal_kb_bot(client)
    cfg = client.get(f"/api/widget/config?key={bot['embed_key']}").get_json()
    assert cfg["mode"] == "internal_kb"
    assert cfg["gated"] is True


def test_chat_blocked_without_unlock_token(client):
    bot = _internal_kb_bot(client)
    resp = client.post("/api/widget/chat", json={
        "key": bot["embed_key"], "message": "how many vacation days?",
        "session_id": uuid.uuid4().hex[:16],
    })
    assert resp.status_code == 401
    assert resp.get_json()["gate_required"] is True


def test_wrong_passphrase_rejected(client):
    bot = _internal_kb_bot(client, passphrase="rightpass")
    resp = client.post("/api/widget/unlock",
                       json={"key": bot["embed_key"], "passphrase": "wrongpass"})
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_correct_passphrase_unlocks_and_chat_returns_citations(client):
    bot = _internal_kb_bot(client, passphrase="rightpass")
    unlock = client.post("/api/widget/unlock",
                         json={"key": bot["embed_key"], "passphrase": "rightpass"})
    assert unlock.status_code == 200
    token = unlock.get_json()["unlock_token"]

    resp = client.post("/api/widget/chat", json={
        "key": bot["embed_key"], "message": "how many vacation days do employees get?",
        "session_id": uuid.uuid4().hex[:16], "unlock_token": token,
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["reply"]
    titles = [c["title"] for c in data["citations"]]
    assert "HR Handbook" in titles
    # Citations carry a snippet + score surfaced from the retrieval.
    assert data["citations"][0]["snippet"]
    assert "score" in data["citations"][0]


def test_public_bots_do_not_emit_citations_by_default(client, bot_id):
    _seed_kb(client, bot_id, "Pricing", "Our starter plan is 49 dollars per month.")
    resp = client.post("/api/widget/chat", json={
        "key": client.get(f"/api/bots/{bot_id}").get_json()["bot"]["embed_key"],
        "message": "how much is the starter plan?", "session_id": uuid.uuid4().hex[:16],
    })
    assert resp.status_code == 200
    assert resp.get_json()["citations"] == []


def test_ungated_internal_kb_bot_is_open(client):
    # internal_kb mode without a passphrase set = not gated (owner hasn't locked it yet).
    bot = client.post("/api/bots", json={"preset": "internal-kb"}).get_json()["bot"]
    cfg = client.get(f"/api/widget/config?key={bot['embed_key']}").get_json()
    assert cfg["gated"] is False
    resp = client.post("/api/widget/chat", json={
        "key": bot["embed_key"], "message": "hello",
        "session_id": uuid.uuid4().hex[:16],
    })
    assert resp.status_code == 200


def test_passphrase_can_be_cleared(client):
    bot = _internal_kb_bot(client, passphrase="temp1234")
    client.delete(f"/api/bots/{bot['id']}/passphrase")
    cfg = client.get(f"/api/widget/config?key={bot['embed_key']}").get_json()
    assert cfg["gated"] is False
