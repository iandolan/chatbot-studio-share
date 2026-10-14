"""Unit tests for the mining engine: extraction, normalization, clustering,
ranking, and weak-answer detection."""
from conftest import GOOD_REPLY, WEAK_REPLY, days_ago, seed_conversation

from server import faq_miner


# ---------- question extraction ----------

def test_extracts_question_mark_sentence():
    assert faq_miner.extract_questions("What are your prices?") == ["What are your prices?"]


def test_extracts_interrogative_without_question_mark():
    assert faq_miner.extract_questions("do you ship to canada") == ["do you ship to canada"]


def test_extracts_imperative_ask():
    qs = faq_miner.extract_questions("Tell me about your refund policy")
    assert qs == ["Tell me about your refund policy"]


def test_skips_small_talk_and_statements():
    assert faq_miner.extract_questions("hi") == []
    assert faq_miner.extract_questions("thanks") == []
    assert faq_miner.extract_questions("That sounds perfect, cheers.") == []


def test_picks_question_sentence_from_multi_sentence_message():
    qs = faq_miner.extract_questions(
        "Hi there. I was on your pricing page. Do you offer annual billing? Thanks!")
    assert qs == ["Do you offer annual billing?"]


def test_strips_greeting_prefix():
    qs = faq_miner.extract_questions("Hey, what are your opening hours?")
    assert qs == ["what are your opening hours?"]


# ---------- normalization ----------

def test_normalize_is_stable_across_case_punctuation_greetings():
    a = faq_miner.normalize_question("Hello!! What Are Your PRICES???")
    b = faq_miner.normalize_question("what are your prices")
    assert a == b == "what are your prices"


# ---------- clustering (via mine) ----------

def test_clusters_near_duplicate_phrasings(app, bot_id):
    variants = [
        "What are your prices?",
        "what's your pricing",
        "Can you tell me the price?",
        "How do prices work?",
    ]
    for v in variants:
        seed_conversation(app, bot_id, [(v, GOOD_REPLY)])
    seed_conversation(app, bot_id, [("Do you ship to Canada?", GOOD_REPLY)])

    with app.app_context():
        clusters = faq_miner.mine(bot_id)
    assert len(clusters) == 2
    top = clusters[0]
    assert top["count"] == 4
    assert "price" in top["cluster_key"] or "pricing" in top["cluster_key"]
    assert len(top["examples"]) <= 3
    assert len(top["members_norm"]) == 4
    assert clusters[1]["count"] == 1


def test_ranking_prefers_recent_over_stale(app, bot_id):
    for _ in range(3):  # 3 recent asks
        seed_conversation(app, bot_id,
                          [("Do you offer a free trial?", GOOD_REPLY)], start=days_ago(1))
    for _ in range(4):  # 4 stale asks
        seed_conversation(app, bot_id,
                          [("Where is your office located?", GOOD_REPLY)], start=days_ago(90))

    with app.app_context():
        clusters = faq_miner.mine(bot_id)
    assert len(clusters) == 2
    assert "trial" in clusters[0]["cluster_key"]      # 3 × 1.0 beats 4 × 0.3
    assert clusters[0]["score"] > clusters[1]["score"]


# ---------- weak-answer detection ----------

def test_weak_flag_for_canned_and_gap_replies(app, bot_id):
    for _ in range(3):
        seed_conversation(app, bot_id, [("Do you integrate with GoHighLevel?", WEAK_REPLY)])
    with app.app_context():
        clusters = faq_miner.mine(bot_id, fallback_message="the team can help")
    (cluster,) = clusters
    assert cluster["weak"] is True
    assert cluster["weak_signals"]["gap"] == 3
    assert cluster["weak_signals"]["canned_reply"] == 3
    assert cluster["best_answer"] == ""  # nothing worth stitching


def test_weak_flag_for_handoff(app, bot_id):
    seed_conversation(app, bot_id, [(
        "Can I change the name on my invoice?",
        {"content": "I've flagged this conversation for the team — they'll pick it up right here.",
         "grounded": 1, "action": {"type": "handoff"}},
    )])
    with app.app_context():
        clusters = faq_miner.mine(bot_id)
    (cluster,) = clusters
    assert cluster["weak"] is True
    assert cluster["weak_signals"]["handoff"] == 1


def test_strong_answers_not_flagged(app, bot_id):
    # Grounded, substantial replies + the visitor kept talking afterwards.
    for _ in range(2):
        seed_conversation(app, bot_id, [
            ("What is your refund policy?", GOOD_REPLY),
            ("perfect, thanks!", {"content": "You're welcome! Anything else I can help with today?",
                                  "grounded": None}),
        ])
    with app.app_context():
        clusters = faq_miner.mine(bot_id)
    (cluster,) = clusters
    assert cluster["weak"] is False
    assert cluster["best_answer"].startswith("We offer a full refund")


def test_mock_mode_suffix_stripped_from_best_answer(app, bot_id):
    seed_conversation(app, bot_id, [(
        "What is your refund policy?",
        {"content": "A: Yes — full refund within 30 days of purchase, no questions asked."
                    "\n\n_(Mock AI mode — answered from your training data. Add an API key "
                    "in .env for real answers.)_",
         "grounded": 1},
    )])
    with app.app_context():
        clusters = faq_miner.mine(bot_id)
    assert clusters[0]["best_answer"] == (
        "Yes — full refund within 30 days of purchase, no questions asked.")


# ---------- drafting fallbacks (mock mode is deterministic) ----------

def test_draft_stitches_best_answer_in_mock_mode(app, bot_id):
    with app.app_context():
        from server import db
        bot = db.query("SELECT * FROM bots WHERE id = ?", (bot_id,), one=True)
        result = faq_miner.draft_answer(bot, "What is your refund policy?",
                                        best_answer="Full refund within 30 days.", count=3)
    assert result == {"answer": "Full refund within 30 days.", "origin": "stitched"}


def test_draft_leaves_todo_when_nothing_to_stitch(app, bot_id):
    with app.app_context():
        from server import db
        bot = db.query("SELECT * FROM bots WHERE id = ?", (bot_id,), one=True)
        result = faq_miner.draft_answer(bot, "Do you support SSO?", best_answer="", count=5)
    assert result["origin"] == "todo"
    assert result["answer"].startswith("TODO")
    assert "5 time(s)" in result["answer"]
