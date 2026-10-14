"""API tests: mine → draft → approve into training → export, plus dismiss and
schema.org JSON-LD validity."""
import json
import re

from conftest import GOOD_REPLY, WEAK_REPLY, seed_conversation


def _mine(client, bot_id):
    resp = client.get(f"/api/bots/{bot_id}/faq/clusters")
    assert resp.status_code == 200
    return resp.get_json()["clusters"]


def _sources(client, bot_id):
    return client.get(f"/api/bots/{bot_id}/sources").get_json()["sources"]


def test_clusters_endpoint_empty_bot(client, bot_id):
    assert _mine(client, bot_id) == []


def test_full_mine_approve_loop_grows_training_data(app, client, bot_id):
    for _ in range(3):
        seed_conversation(app, bot_id, [("Do you offer white label plans?", WEAK_REPLY)])

    clusters = _mine(client, bot_id)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["weak"] is True

    # draft (mock mode → deterministic TODO since the bot never answered well)
    resp = client.post(f"/api/bots/{bot_id}/faq/draft", json={
        "question": cluster["question"], "best_answer": cluster["best_answer"],
        "count": cluster["count"],
    })
    assert resp.status_code == 200
    assert resp.get_json()["origin"] == "todo"

    # approving a TODO answer is rejected
    resp = client.post(f"/api/bots/{bot_id}/faq/approve", json={
        "question": cluster["question"], "answer": resp.get_json()["answer"],
    })
    assert resp.status_code == 400

    # approve with a real answer → new qa training source, indexed
    sources_before = _sources(client, bot_id)
    resp = client.post(f"/api/bots/{bot_id}/faq/approve", json={
        "question": cluster["question"],
        "answer": "Yes — white label is available on the agency plan.",
        "members_norm": cluster["members_norm"],
    })
    assert resp.status_code == 201
    entry = resp.get_json()["entry"]
    sources_after = _sources(client, bot_id)
    assert len(sources_after) == len(sources_before) + 1
    new_source = sources_after[0]
    assert new_source["type"] == "qa"
    assert new_source["chunk_count"] >= 1          # embedded in mock mode too
    assert new_source["id"] == entry["source_id"]

    # entry is listed; the cluster is suppressed from future mining
    entries = client.get(f"/api/bots/{bot_id}/faq/entries").get_json()["entries"]
    assert [e["id"] for e in entries] == [entry["id"]]
    assert _mine(client, bot_id) == []

    # the approved Q&A is retrievable by the RAG layer (the bot actually learned)
    with app.app_context():
        from server import rag
        chunks, best = rag.retrieve(bot_id, "do you have white label plans?")
    assert best > 0.3
    assert "agency plan" in chunks[0]["content"]


def test_new_phrasing_of_approved_question_stays_suppressed(app, client, bot_id):
    seed_conversation(app, bot_id, [("Do you integrate with Zapier?", WEAK_REPLY)])
    (cluster,) = _mine(client, bot_id)
    client.post(f"/api/bots/{bot_id}/faq/approve", json={
        "question": cluster["question"], "answer": "Yes, native Zapier integration.",
        "members_norm": cluster["members_norm"]})
    # a NEW phrasing arrives after approval — fuzzy suppression should catch it
    seed_conversation(app, bot_id, [("do you support zapier?", WEAK_REPLY)])
    assert _mine(client, bot_id) == []


def test_dismiss_suppresses_cluster(app, client, bot_id):
    seed_conversation(app, bot_id, [("Can I pay with crypto?", WEAK_REPLY)])
    (cluster,) = _mine(client, bot_id)
    resp = client.post(f"/api/bots/{bot_id}/faq/dismiss",
                       json={"members_norm": cluster["members_norm"]})
    assert resp.status_code == 200
    assert _mine(client, bot_id) == []
    # dismissing nothing is a 400
    assert client.post(f"/api/bots/{bot_id}/faq/dismiss", json={}).status_code == 400


def test_delete_entry_keeps_training_source(app, client, bot_id):
    client.post(f"/api/bots/{bot_id}/faq/approve", json={
        "question": "Do you offer support?", "answer": "Yes, 24/7 via chat."})
    entries = client.get(f"/api/bots/{bot_id}/faq/entries").get_json()["entries"]
    resp = client.delete(f"/api/bots/{bot_id}/faq/entries/{entries[0]['id']}")
    assert resp.status_code == 200
    assert client.get(f"/api/bots/{bot_id}/faq/entries").get_json()["entries"] == []
    assert len(_sources(client, bot_id)) == 1  # qa source survives


def test_export_html_contains_valid_faqpage_json_ld(client, bot_id):
    qa = [
        ("What are your prices?", 'Plans start at $29/month — see the <pricing> page & ask us anything.'),
        ("Do you offer refunds?", "Yes.\nFull refund within 30 days."),
    ]
    for q, a in qa:
        assert client.post(f"/api/bots/{bot_id}/faq/approve",
                           json={"question": q, "answer": a}).status_code == 201

    resp = client.get(f"/api/bots/{bot_id}/faq/export?format=html")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    # markup: escaped, one item per FAQ (JSON-LD carries the raw text)
    assert html.count('class="faq-item"') == 2
    markup = html.split('<script type="application/ld+json">')[0]
    assert "&lt;pricing&gt;" in markup and "<pricing>" not in markup
    assert "<br>" in markup  # newline in the refund answer rendered as <br>

    # JSON-LD: parses, right shape, right content
    match = re.search(r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
                      html, re.S)
    assert match, "JSON-LD script block missing"
    data = json.loads(match.group(1).replace("<\\/", "</"))
    assert data["@context"] == "https://schema.org"
    assert data["@type"] == "FAQPage"
    assert len(data["mainEntity"]) == 2
    first = data["mainEntity"][0]
    assert first["@type"] == "Question"
    assert first["name"] == "What are your prices?"
    assert first["acceptedAnswer"]["@type"] == "Answer"
    assert "$29/month" in first["acceptedAnswer"]["text"]


def test_export_markdown(client, bot_id):
    client.post(f"/api/bots/{bot_id}/faq/approve",
                json={"question": "Do you offer refunds?",
                      "answer": "Yes — within 30 days."})
    resp = client.get(f"/api/bots/{bot_id}/faq/export?format=markdown")
    assert resp.status_code == 200
    md = resp.get_data(as_text=True)
    assert "## Frequently asked questions" in md
    assert "### Do you offer refunds?" in md
    assert "Yes — within 30 days." in md


def test_faq_endpoints_404_for_unknown_bot(client):
    assert client.get("/api/bots/999999/faq/clusters").status_code == 404
    assert client.post("/api/bots/999999/faq/draft",
                       json={"question": "x?"}).status_code == 404
    assert client.get("/api/bots/999999/faq/export").status_code == 404
