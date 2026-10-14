"""FAQ / knowledge-base miner.

Mines a bot's real visitor messages for questions, clusters near-duplicate
phrasings (token-overlap Jaccard, with an optional embedding-assisted merge
pass through the existing mock-safe embeddings layer), ranks clusters by
frequency + recency, and flags clusters where the bot's answers looked weak
(gaps, canned/short replies, human handoffs, visitors bouncing right after).

Everything here is deterministic without API keys: in mock mode the embedding
pass uses the hashed bag-of-words vectors from llm.embed_texts, and
draft_answer() falls back to stitching the bot's best existing reply (or a
TODO placeholder) instead of calling an LLM.
"""
import json
import math
import re
from datetime import datetime, timezone
from html import escape as html_escape

from . import db, rag
from .config import Config
from .llm import LLMError, chat_completion, embed_texts

SIM_THRESHOLD = 0.42         # token-overlap similarity to join a cluster
                             # (0.42 lets two-token questions sharing their
                             #  key noun group: containment 0.5 * 0.9 = 0.45)
EMBED_MERGE_THRESHOLD = 0.86  # cosine similarity to merge two clusters
WEAK_THRESHOLD = 0.5          # avg weakness score at which a cluster is flagged
SHORT_REPLY_CHARS = 50
MAX_EXAMPLES = 3

# ---------- question extraction ----------

INTERROGATIVES = (
    "what", "whats", "what's", "how", "why", "when", "where", "who", "whos",
    "who's", "which", "can", "could", "do", "does", "did", "is", "are",
    "was", "were", "will", "would", "should", "shall", "may", "might", "am",
    "has", "have",
)

IMPERATIVE_ASKS = (
    "tell me", "explain", "help me", "show me", "give me", "send me",
    "i need", "i want", "i'd like", "id like", "i am looking for",
    "i'm looking for", "im looking for", "looking for", "let me know",
    "walk me through", "wondering",
)

SMALL_TALK = {
    "hi", "hello", "hey", "yo", "thanks", "thank you", "thx", "ok", "okay",
    "yes", "no", "yep", "nope", "cool", "great", "nice", "bye", "goodbye",
    "test", "testing", "sure", "hmm", "lol", "good", "fine",
}

_GREETING_PREFIX = re.compile(
    r"^(hi|hello|hey|good\s+(morning|afternoon|evening))\s*[,!.\s]+", re.I)
_SENTENCE_SPLIT = re.compile(r"(?<=[.?!])\s+|\n+")
_TOKEN_RE = re.compile(r"[a-z0-9']+")

STOPWORDS = {
    "a", "an", "the", "i", "you", "your", "yours", "my", "me", "we", "our",
    "us", "it", "its", "is", "are", "was", "were", "be", "been", "am",
    "do", "does", "did", "can", "could", "would", "will", "shall", "should",
    "may", "might", "have", "has", "had", "of", "to", "for", "in", "on",
    "at", "and", "or", "if", "so", "there", "this", "that", "these", "those",
    "with", "about", "any", "some", "please", "pls", "just", "hi", "hello",
    "hey", "guys", "u", "ur",
    # interrogative/function words carry no topical meaning for clustering
    "what", "whats", "what's", "how", "why", "when", "where", "who", "whos",
    "who's", "which", "tell", "me", "much", "many", "know",
}


def _stem(token):
    """Crude suffix stemmer, enough to cluster 'prices'/'pricing'/'price'."""
    if len(token) > 4:
        for suffix in ("ing", "edly", "ed", "es", "s", "ly"):
            if token.endswith(suffix):
                token = token[: -len(suffix)]
                break
    if len(token) > 3 and token.endswith("e"):
        token = token[:-1]
    if len(token) > 3 and token[-1] == token[-2]:
        token = token[:-1]
    return token


def extract_questions(text):
    """Pull question-like sentences out of one visitor message.

    Catches explicit '?' sentences, interrogative-first sentences, and
    imperative asks ("tell me about…"). Skips greetings/small talk.
    """
    text = _GREETING_PREFIX.sub("", (text or "").strip())
    if not text:
        return []
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    questions = [s for s in sentences if s.endswith("?")]
    if not questions:
        for sentence in sentences:
            lower = sentence.lower()
            first_word = (_TOKEN_RE.findall(lower) or [""])[0]
            if first_word in INTERROGATIVES or lower.startswith(IMPERATIVE_ASKS):
                questions.append(sentence)
                break  # one implicit question per message is plenty
    cleaned = []
    for q in questions:
        norm = normalize_question(q)
        if len(norm) < 8 or norm in SMALL_TALK:
            continue
        if len(_TOKEN_RE.findall(norm)) < 2:
            continue
        cleaned.append(q)
    return cleaned


def normalize_question(question):
    """Stable lowercase form used as the cluster/suppression key."""
    q = _GREETING_PREFIX.sub("", (question or "").strip()).lower()
    q = re.sub(r"^(please|pls|just)\s+", "", q)
    q = re.sub(r"[^a-z0-9\s']", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def _token_set(norm):
    return {_stem(t) for t in _TOKEN_RE.findall(norm)
            if t not in STOPWORDS and len(t) > 1}


def _similarity(tokens_a, tokens_b):
    if not tokens_a or not tokens_b:
        return 0.0
    inter = len(tokens_a & tokens_b)
    if not inter:
        return 0.0
    jaccard = inter / len(tokens_a | tokens_b)
    containment = inter / min(len(tokens_a), len(tokens_b))
    return max(jaccard, 0.9 * containment)


# ---------- weak-answer detection ----------

CANNED_PATTERNS = (
    "i don't know", "i dont know", "don't have that information",
    "dont have that information", "i'm not sure", "im not sure",
    "i am not sure", "can't help with", "cannot help with",
    "mock ai mode", "no matching knowledge", "unable to answer",
    "don't have details", "dont have details",
)

# signal name -> weight toward the occurrence's weakness score
SIGNAL_WEIGHTS = {
    "no_reply": 1.0,
    "gap": 1.0,            # retrieval found nothing (messages.grounded = 0)
    "handoff": 1.0,        # handoff action fired or a human had to answer
    "canned_reply": 1.0,   # fallback / "I don't know" style reply
    "short_reply": 0.6,
    "ended_after": 0.4,    # visitor never wrote again after this answer
}


def _reply_signals(occ, fallback_message):
    """Set of weak-answer signal names for one question occurrence."""
    signals = set()
    if not (occ.get("reply") or "").strip():
        return {"no_reply"}
    # judge the substance of the reply, not mock-mode decoration
    reply = clean_answer_text(occ["reply"])
    lower = reply.lower()
    if occ.get("grounded") == 0:
        signals.add("gap")
    if occ.get("handoff"):
        signals.add("handoff")
    if any(p in lower for p in CANNED_PATTERNS) or (
            fallback_message and fallback_message.lower() in lower):
        signals.add("canned_reply")
    if len(reply.strip()) < SHORT_REPLY_CHARS:
        signals.add("short_reply")
    if occ.get("ended_after"):
        signals.add("ended_after")
    return signals


def _occurrence_weakness(signals):
    return min(1.0, sum(SIGNAL_WEIGHTS[s] for s in signals))


_MOCK_SUFFIX = re.compile(r"\s*_\(Mock AI mode[^)]*\)_\s*$")


def clean_answer_text(text):
    """Strip mock-mode decoration / echoed source headers / Q&A prefixes from
    a bot reply so it can be stitched into an FAQ draft."""
    text = _MOCK_SUFFIX.sub("", (text or "").strip()).strip()
    text = "\n".join(ln for ln in text.splitlines()
                     if not re.match(r"^\s*\[Source:.*\]\s*$", ln)).strip()
    if text.startswith("Q:") and "\nA:" in text:
        text = text.split("\nA:", 1)[1].strip()
    elif text.startswith("A:"):
        text = text[2:].strip()
    return text


# ---------- recency-weighted ranking ----------

def _parse_ts(value):
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _recency_weight(created_at, now):
    ts = _parse_ts(created_at)
    if ts is None:
        return 0.3
    age_days = max(0.0, (now - ts).total_seconds() / 86400)
    if age_days <= 7:
        return 1.0
    if age_days <= 30:
        return 0.6
    return 0.3


# ---------- mining ----------

def _collect_occurrences(bot_id, fallback_message, suppressed):
    rows = db.query(
        """SELECT m.conversation_id, m.role, m.content, m.action, m.grounded, m.created_at
           FROM messages m JOIN conversations c ON c.id = m.conversation_id
           WHERE c.bot_id = ? ORDER BY m.conversation_id, m.id""",
        (bot_id,),
    )
    convos = {}
    for row in rows:
        convos.setdefault(row["conversation_id"], []).append(row)

    suppressed_tokens = [t for t in (_token_set(n) for n in suppressed) if t]
    occurrences = []
    for msgs in convos.values():
        for i, msg in enumerate(msgs):
            if msg["role"] != "user":
                continue
            questions = extract_questions(msg["content"])
            if not questions:
                continue
            reply_row = next(
                (m for m in msgs[i + 1:] if m["role"] in ("assistant", "agent")), None)
            later_user = any(m["role"] == "user" for m in msgs[i + 1:])
            handoff = bool(reply_row and (
                reply_row["role"] == "agent"
                or '"handoff"' in (reply_row["action"] or "")))
            for question in questions:
                norm = normalize_question(question)
                if not norm or norm in suppressed:
                    continue
                tokens = _token_set(norm)
                if not tokens:
                    continue
                # fuzzy suppression: new phrasings of an already approved or
                # dismissed question shouldn't resurface
                if any(_similarity(tokens, sup) >= SIM_THRESHOLD
                       for sup in suppressed_tokens):
                    continue
                occ = {
                    "question": question.strip(),
                    "norm": norm,
                    "tokens": tokens,
                    "created_at": msg["created_at"],
                    "reply": reply_row["content"] if reply_row else "",
                    "grounded": reply_row["grounded"] if reply_row else None,
                    "handoff": handoff,
                    "ended_after": not later_user,
                }
                occ["signals"] = _reply_signals(occ, fallback_message)
                occurrences.append(occ)
    occurrences.sort(key=lambda o: o["created_at"])
    return occurrences


def _cluster_occurrences(occurrences):
    """Greedy token-overlap clustering; each cluster is a list of occurrences."""
    clusters = []       # list of lists
    cluster_tokens = [] # list of {norm: tokens} per cluster (unique variants)
    for occ in occurrences:
        best_idx, best_sim = -1, 0.0
        for idx, variants in enumerate(cluster_tokens):
            sim = max(_similarity(occ["tokens"], t) for t in variants.values())
            if sim > best_sim:
                best_idx, best_sim = idx, sim
        if best_idx >= 0 and best_sim >= SIM_THRESHOLD:
            clusters[best_idx].append(occ)
            cluster_tokens[best_idx].setdefault(occ["norm"], occ["tokens"])
        else:
            clusters.append([occ])
            cluster_tokens.append({occ["norm"]: occ["tokens"]})
    return clusters


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


def _embedding_merge(clusters):
    """Optional second pass: merge clusters whose representative questions
    embed very close together. Mock-safe (deterministic hashed embeddings);
    silently skipped if the embeddings layer errors."""
    if len(clusters) < 2:
        return clusters
    reps = [max(cl, key=lambda o: len(o["norm"]))["norm"] for cl in clusters]
    try:
        vectors = embed_texts(reps)
    except LLMError:
        return clusters
    parent = list(range(len(clusters)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            if find(i) != find(j) and _cosine(vectors[i], vectors[j]) >= EMBED_MERGE_THRESHOLD:
                parent[find(j)] = find(i)

    merged = {}
    for idx, cluster in enumerate(clusters):
        merged.setdefault(find(idx), []).extend(cluster)
    return list(merged.values())


def _best_answer(cluster):
    """Longest confident bot reply in the cluster (for draft stitching)."""
    candidates = []
    for occ in cluster:
        if not occ["reply"] or occ["handoff"]:
            continue
        if "canned_reply" in occ["signals"] or occ.get("grounded") == 0:
            continue
        cleaned = clean_answer_text(occ["reply"])
        if cleaned:
            candidates.append(cleaned)
    return max(candidates, key=len) if candidates else ""


def _summarize_cluster(cluster, now):
    # representative = most frequent normalized variant (ties: most recent)
    by_norm = {}
    for occ in cluster:
        by_norm.setdefault(occ["norm"], []).append(occ)
    rep_norm = max(by_norm, key=lambda n: (len(by_norm[n]),
                                           max(o["created_at"] for o in by_norm[n])))
    rep_occ = max(by_norm[rep_norm], key=lambda o: o["created_at"])

    seen, examples = set(), []
    for occ in sorted(cluster, key=lambda o: o["created_at"], reverse=True):
        if occ["norm"] in seen:
            continue
        seen.add(occ["norm"])
        examples.append(occ["question"])
        if len(examples) >= MAX_EXAMPLES:
            break

    signal_counts = {name: 0 for name in SIGNAL_WEIGHTS}
    weakness_total = 0.0
    for occ in cluster:
        weakness_total += _occurrence_weakness(occ["signals"])
        for s in occ["signals"]:
            signal_counts[s] += 1
    weak_score = weakness_total / len(cluster)

    return {
        "cluster_key": rep_norm,
        "question": rep_occ["question"],
        "count": len(cluster),
        "score": round(sum(_recency_weight(o["created_at"], now) for o in cluster), 2),
        "last_seen": max(o["created_at"] for o in cluster),
        "examples": examples,
        "members_norm": sorted(by_norm.keys()),
        "weak": weak_score >= WEAK_THRESHOLD,
        "weak_score": round(weak_score, 2),
        "weak_signals": {k: v for k, v in signal_counts.items() if v},
        "best_answer": _best_answer(cluster),
    }


def mine(bot_id, fallback_message="", limit=40, now=None):
    """Mine + cluster + rank a bot's visitor questions.

    Returns a ranked list of cluster dicts (see _summarize_cluster). Questions
    already approved into training or dismissed (faq_suppressed) are excluded.
    """
    now = now or datetime.now(timezone.utc)
    suppressed = {r["question_norm"] for r in db.query(
        "SELECT question_norm FROM faq_suppressed WHERE bot_id = ?", (bot_id,))}
    occurrences = _collect_occurrences(bot_id, fallback_message, suppressed)
    if not occurrences:
        return []
    clusters = _embedding_merge(_cluster_occurrences(occurrences))
    summaries = [_summarize_cluster(cl, now) for cl in clusters]
    summaries.sort(key=lambda c: (c["score"], c["count"], c["last_seen"]), reverse=True)
    return summaries[:limit]


# ---------- FAQ answer drafting (LLM with deterministic fallback) ----------

DRAFT_SYSTEM = (
    "You write website FAQ answers for the business described below. "
    "Answer ONLY from the KNOWLEDGE section; never invent facts, prices or "
    "policies. Write 1-3 short sentences in plain language, addressed to a "
    "website visitor. Reply with the answer text only — no preamble, no "
    "markdown headings.\n\n# BUSINESS CONTEXT\n{context}\n\n# KNOWLEDGE\n{knowledge}"
)


def draft_answer(bot, question, best_answer="", count=1):
    """Draft an FAQ answer for a mined question cluster.

    Real mode: asks the bot's LLM, grounded in retrieved training chunks.
    Mock mode / LLM failure: deterministically stitches the bot's best
    existing reply, or leaves a TODO for the owner. Returns
    {"answer": str, "origin": "llm" | "stitched" | "todo"}.
    """
    chunks = []
    if not Config.MOCK_LLM:
        try:
            chunks, _ = rag.retrieve(bot["id"], question)
        except LLMError:
            chunks = []
        from .chat import bot_credentials  # local import: chat also imports rag/llm
        provider, api_key, model = bot_credentials(bot)
        system = DRAFT_SYSTEM.format(
            context=(bot["system_prompt"] or bot["name"]).strip()[:2000],
            knowledge=rag.knowledge_block(chunks),
        )
        try:
            result = chat_completion(
                provider, api_key, model, system,
                [{"role": "user", "content":
                  f"Write the FAQ answer for: {question}"}],
                temperature=0.2, max_tokens=400,
            )
            text = clean_answer_text(result["text"])
            if text:
                return {"answer": text, "origin": "llm"}
        except LLMError:
            pass
    stitched = clean_answer_text(best_answer)
    if stitched:
        return {"answer": stitched, "origin": "stitched"}
    return {
        "answer": (f"TODO: write this answer. Visitors asked {count} time(s) "
                   "and the bot had no confident reply."),
        "origin": "todo",
    }


# ---------- website FAQ export ----------

def export_markdown(entries):
    parts = ["## Frequently asked questions", ""]
    for entry in entries:
        parts += [f"### {entry['question']}", "", entry["answer"], ""]
    return "\n".join(parts).rstrip() + "\n"


def export_html(bot_name, entries):
    """Copy-paste HTML block with schema.org FAQPage JSON-LD (AEO-friendly)."""
    items = []
    main_entity = []
    for entry in entries:
        q, a = entry["question"], entry["answer"]
        items.append(
            '  <div class="faq-item">\n'
            f"    <h3>{html_escape(q)}</h3>\n"
            f"    <p>{html_escape(a).replace(chr(10), '<br>')}</p>\n"
            "  </div>"
        )
        main_entity.append({
            "@type": "Question",
            "name": q,
            "acceptedAnswer": {"@type": "Answer", "text": a},
        })
    json_ld = json.dumps(
        {"@context": "https://schema.org", "@type": "FAQPage",
         "mainEntity": main_entity},
        ensure_ascii=False, indent=2,
    ).replace("</", "<\\/")  # keep </script> from breaking the block
    return (
        f"<!-- FAQ section generated by Chatbot Studio from real visitor "
        f"questions ({html_escape(bot_name)}) -->\n"
        '<section class="faq-section">\n'
        "  <h2>Frequently asked questions</h2>\n"
        + "\n".join(items) + "\n"
        '  <script type="application/ld+json">\n'
        f"{json_ld}\n"
        "  </script>\n"
        "</section>\n"
    )
