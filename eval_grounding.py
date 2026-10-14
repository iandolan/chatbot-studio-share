#!/usr/bin/env python3
"""Grounding eval for a chatbot-studio bot — the corpus-abstention scorecard.

The failure this hunts is NOT "bot hallucinates on off-topic questions"
(the closed-book system prompt handles that). It's PARTIAL COVERAGE:
retrieval returns chunks that are topically right but don't cover the whole
question, and the model completes the remainder from its own weights. The
answer cites real material, reads correct, and contains a claim the site
never made. Nothing in the transcript flags it — you need an eval that
tests for it on purpose.

What it does, per run:
1. Pulls the bot's full training corpus (chunks) straight from SQLite.
2. Generates N test questions with an LLM, partitioned by failure surface:
     in_corpus              — fully answerable from the corpus
     boundary               — topic covered, the specific detail asked is NOT
     out_of_corpus_plausible— plausible for this business, absent from corpus
     off_domain             — unrelated to the business entirely
3. Asks the bot each question over the real widget API (rate-limit paced).
4. Judges each answer against the corpus into one verdict:
     GROUNDED_ANSWER / CORRECT_ABSTENTION / UNGROUNDED_ANSWER /
     MIXED_LEAKAGE / WRONG_ABSTENTION
5. Prints + saves a scorecard. SHOW-READY means zero UNGROUNDED_ANSWER and
   zero MIXED_LEAKAGE — the two verdicts that can burn a demo.

Usage (server must be running, default http://127.0.0.1:5090):
    .venv/bin/python eval_grounding.py --bot pk_YOUR_BOT_KEY
    .venv/bin/python eval_grounding.py --bot "My Bot" --n 20

Needs a real provider key in .env (judging in mock mode is meaningless);
judge/generator calls count against GLOBAL_DAILY_LLM_CALLS (~n+2 calls).
Results land in data/evals/.
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask

from server.config import Config
from server.llm import LLMError, chat_completion, embed_texts

BASE_DIR = Path(__file__).resolve().parent
EVAL_DIR = BASE_DIR / "data" / "evals"

VERDICTS = ("GROUNDED_ANSWER", "CORRECT_ABSTENTION", "UNGROUNDED_ANSWER",
            "MIXED_LEAKAGE", "WRONG_ABSTENTION")

# Which verdicts count as a pass, per question partition.
PASS_VERDICTS = {
    "in_corpus": {"GROUNDED_ANSWER"},
    "boundary": {"GROUNDED_ANSWER", "CORRECT_ABSTENTION"},
    "out_of_corpus_plausible": {"CORRECT_ABSTENTION"},
    "off_domain": {"CORRECT_ABSTENTION"},
}
# The two verdicts that make a bot NOT show-ready regardless of totals.
FATAL_VERDICTS = {"UNGROUNDED_ANSWER", "MIXED_LEAKAGE"}

# Harness guardrails (the loop needs exits too).
TOTAL_DEADLINE_S = 20 * 60          # whole eval wall clock
CHAT_PACE_S = 3.5                   # widget API allows 20/min per IP
MAX_CONSECUTIVE_CHAT_FAILURES = 4   # server down / systemic → abort, don't grind
CORPUS_INLINE_LIMIT = 30_000        # chars; larger corpora fall back to top-K


def parse_args():
    p = argparse.ArgumentParser(description="Grounding eval for one bot")
    p.add_argument("--bot", required=True,
                   help="embed key (pk_...), numeric bot id, or exact bot name")
    p.add_argument("--n", type=int, default=20,
                   help="number of questions (default 20, max 40)")
    p.add_argument("--base-url", default="http://127.0.0.1:5090")
    p.add_argument("--model", default=None,
                   help="judge/generator model override")
    p.add_argument("--out", default=str(EVAL_DIR))
    return p.parse_args()


# ── data access ──────────────────────────────────────────────────────────────

def open_db():
    conn = sqlite3.connect(f"file:{Config.DATABASE_PATH}?mode=ro", uri=True,
                           timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def find_bot(conn, ident: str) -> sqlite3.Row:
    for sql, arg in (
        ("SELECT * FROM bots WHERE embed_key = ?", ident),
        ("SELECT * FROM bots WHERE id = ?", ident),
        ("SELECT * FROM bots WHERE name = ? COLLATE NOCASE", ident),
    ):
        row = conn.execute(sql, (arg,)).fetchone()
        if row:
            return row
    sys.exit(f"No bot matches {ident!r} (tried embed key, id, name)")


def load_corpus(conn, bot_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT c.content, c.embedding, s.title
           FROM chunks c JOIN sources s ON s.id = c.source_id
           WHERE c.bot_id = ? ORDER BY c.source_id, c.position""",
        (bot_id,)).fetchall()
    return [{"content": r["content"], "title": r["title"] or "untitled",
             "embedding": r["embedding"]} for r in rows]


def corpus_text(corpus: list[dict], limit: int | None = None) -> str:
    parts = [f"[Source: {c['title']}]\n{c['content']}" for c in corpus]
    text = "\n\n---\n\n".join(parts)
    return text[:limit] if limit else text


def top_chunks(corpus: list[dict], question: str, k: int = 8) -> list[dict]:
    """Cosine top-K against stored embeddings (same math as rag.retrieve,
    minus the Flask dependency). Falls back to the full corpus on any issue."""
    try:
        import numpy as np
        qv = np.array(embed_texts([question])[0], dtype=np.float32)
        usable = [c for c in corpus
                  if c["embedding"] and len(c["embedding"]) == len(qv) * 4]
        if not usable:
            return corpus
        matrix = np.vstack([np.frombuffer(c["embedding"], dtype=np.float32)
                            for c in usable])
        norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(qv) or 1.0)
        norms[norms == 0] = 1.0
        scores = (matrix @ qv) / norms
        order = np.argsort(-scores)[:k]
        return [usable[int(i)] for i in order]
    except Exception:
        return corpus


# ── LLM plumbing (generator + judge) ─────────────────────────────────────────

def pick_judge(model_override: str | None) -> tuple[str, str, str]:
    """(provider, api_key, model) for the generator/judge calls."""
    if Config.OPENROUTER_API_KEY:
        return ("openrouter", Config.OPENROUTER_API_KEY,
                model_override or "anthropic/claude-sonnet-4.5")
    if Config.ANTHROPIC_API_KEY:
        return ("anthropic", Config.ANTHROPIC_API_KEY,
                model_override or "claude-sonnet-4-5")
    if Config.OPENAI_API_KEY:
        return ("openai", Config.OPENAI_API_KEY, model_override or "gpt-4o")
    sys.exit("No provider API key in .env — a mock-mode judge is meaningless.")


def llm_json(provider, key, model, system, user, max_tokens=2048):
    """One completion parsed as JSON, tolerant of code fences."""
    res = chat_completion(provider, key, model, system,
                          [{"role": "user", "content": user}],
                          temperature=0.3, max_tokens=max_tokens,
                          json_mode=True)
    text = res["text"].strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"No JSON object in response: {text[:200]}")
    return json.loads(text[start:end + 1])


GEN_SYSTEM = """You design evaluation questions for a website chatbot. \
You will be shown the bot's ENTIRE knowledge corpus (crawled from one \
business's website). Write natural visitor-style questions, partitioned \
exactly as requested. Answer ONLY with JSON."""

GEN_USER = """Business: {name}

KNOWLEDGE CORPUS (complete):
{corpus}

Write exactly {n} short, natural questions a website visitor might type, split:
- {n_in} with partition "in_corpus": fully answerable from the corpus above.
- {n_bd} with partition "boundary": the TOPIC is covered in the corpus but the
  SPECIFIC detail asked (a price, a deadline, a guarantee, a named person,
  a location, a percentage) is NOT stated anywhere in it. These are the
  dangerous ones — pick topics the corpus genuinely touches.
- {n_op} with partition "out_of_corpus_plausible": something this kind of
  business could plausibly be asked but the corpus never covers at all.
- {n_od} with partition "off_domain": unrelated to the business entirely
  (news, recipes, coding help, celebrity trivia...).

JSON only:
{{"questions": [{{"partition": "...", "question": "...",
   "note": "for in_corpus: the corpus fact that answers it; for boundary:
            what IS covered vs which detail is missing; else short rationale"}}]}}"""


JUDGE_SYSTEM = """You are a strict grounding auditor for a website chatbot. \
The bot must answer ONLY from its knowledge corpus and abstain otherwise. \
Compare the bot's answer against the corpus excerpt and classify it. Judge \
CLAIMS about the business/world, not phrasing. Polite deflection to contact \
the business counts as abstention. Answer ONLY with JSON."""

JUDGE_USER = """QUESTION ({partition}): {question}
Question designer's note: {note}

BOT'S ANSWER:
{answer}

KNOWLEDGE CORPUS the bot had access to{scope_note}:
{corpus}

Classify into exactly one verdict:
- GROUNDED_ANSWER: every factual claim in the answer is supported by the corpus.
- CORRECT_ABSTENTION: the bot declined / said it doesn't have that info,
  and the corpus indeed doesn't cover what was asked.
- UNGROUNDED_ANSWER: the answer's substance comes from outside the corpus.
- MIXED_LEAKAGE: mostly grounded BUT at least one factual claim (a price,
  number, name, promise, capability...) is not supported by the corpus.
- WRONG_ABSTENTION: the bot declined although the corpus contains the answer.

JSON only:
{{"verdict": "...", "unsupported_claims": ["exact claim not in corpus", ...],
  "reasoning": "one or two sentences"}}"""


# ── the bot under test ───────────────────────────────────────────────────────

def ask_bot(base_url: str, embed_key: str, question: str, session_id: str) -> str:
    payload = json.dumps({"key": embed_key, "message": question,
                          "session_id": session_id}).encode()
    req = urllib.request.Request(
        f"{base_url}/api/widget/chat", data=payload, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return (json.load(resp).get("reply") or "").strip()


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    n = max(8, min(args.n, 40))
    # partition counts: 40/20/20/20, remainder to in_corpus
    n_bd = n_op = n_od = n // 5
    n_in = n - n_bd - n_op - n_od

    def _deadline(signum, frame):
        sys.exit(f"[GUARD] eval exceeded {TOTAL_DEADLINE_S}s wall clock — aborted")
    signal.signal(signal.SIGALRM, _deadline)
    signal.alarm(TOTAL_DEADLINE_S)

    conn = open_db()
    bot = find_bot(conn, args.bot)
    corpus = load_corpus(conn, bot["id"])
    conn.close()
    if not corpus:
        sys.exit(f"Bot {bot['name']!r} has no training chunks — train it first.")

    provider, key, model = pick_judge(args.model)
    full_corpus = corpus_text(corpus)
    small = len(full_corpus) <= CORPUS_INLINE_LIMIT
    print(f"Bot: {bot['name']} (id {bot['id']}) — {len(corpus)} chunks, "
          f"{len(full_corpus)} chars | judge: {provider}/{model} | n={n}")

    # 1. generate questions
    print("Generating questions...")
    gen = llm_json(provider, key, model, GEN_SYSTEM, GEN_USER.format(
        name=bot["name"], corpus=full_corpus[:60_000], n=n,
        n_in=n_in, n_bd=n_bd, n_op=n_op, n_od=n_od), max_tokens=4096)
    questions = [q for q in gen.get("questions", [])
                 if q.get("partition") in PASS_VERDICTS and q.get("question")]
    if len(questions) < n // 2:
        sys.exit(f"Question generation returned only {len(questions)} usable "
                 f"questions — not enough for a meaningful scorecard.")

    # 2+3. ask the bot, then judge each answer
    run_tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    results, streak = [], 0
    for i, q in enumerate(questions, 1):
        try:
            answer = ask_bot(args.base_url, bot["embed_key"], q["question"],
                             f"eval-{run_tag}-{i}")
            streak = 0
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            streak += 1
            print(f"  [{i}/{len(questions)}] chat failed ({e}) — "
                  f"streak {streak}/{MAX_CONSECUTIVE_CHAT_FAILURES}")
            if streak >= MAX_CONSECUTIVE_CHAT_FAILURES:
                sys.exit("[GUARD] consecutive chat failures — is the server "
                         f"running at {args.base_url}? Aborting.")
            time.sleep(30)
            continue

        ctx = full_corpus if small else corpus_text(
            top_chunks(corpus, q["question"]))
        scope = "" if small else " (top retrieved chunks only)"
        try:
            j = llm_json(provider, key, model, JUDGE_SYSTEM, JUDGE_USER.format(
                partition=q["partition"], question=q["question"],
                note=q.get("note", ""), answer=answer or "(empty reply)",
                corpus=ctx, scope_note=scope))
            verdict = j.get("verdict", "")
            if verdict not in VERDICTS:
                raise LLMError(f"bad verdict {verdict!r}")
        except (LLMError, json.JSONDecodeError, ValueError) as e:
            j = {"verdict": "JUDGE_ERROR", "unsupported_claims": [],
                 "reasoning": str(e)}
            verdict = "JUDGE_ERROR"

        passed = verdict in PASS_VERDICTS.get(q["partition"], set())
        results.append({"partition": q["partition"], "question": q["question"],
                        "note": q.get("note", ""), "answer": answer,
                        "verdict": verdict, "passed": passed,
                        "unsupported_claims": j.get("unsupported_claims", []),
                        "reasoning": j.get("reasoning", "")})
        mark = "✓" if passed else "✗"
        print(f"  [{i}/{len(questions)}] {mark} {verdict:18s} "
              f"({q['partition']}) {q['question'][:60]}")
        time.sleep(CHAT_PACE_S)

    signal.alarm(0)
    report(bot, results, provider, model, run_tag, Path(args.out))


def report(bot, results, provider, model, run_tag, out_dir: Path):
    scored = [r for r in results if r["verdict"] != "JUDGE_ERROR"]
    passed = sum(1 for r in scored if r["passed"])
    fatal = [r for r in scored if r["verdict"] in FATAL_VERDICTS]
    show_ready = bool(scored) and not fatal

    lines = [f"# Grounding eval — {bot['name']}",
             f"Run: {run_tag} UTC | judge: {provider}/{model}", "",
             f"**Score: {passed}/{len(scored)}**  |  "
             f"**{'SHOW-READY ✅' if show_ready else 'NOT SHOW-READY ❌'}** "
             f"({len(fatal)} unsupported-claim answer(s))", "",
             "| partition | pass | total |", "|---|---|---|"]
    for part in PASS_VERDICTS:
        sub = [r for r in scored if r["partition"] == part]
        if sub:
            lines.append(f"| {part} | "
                         f"{sum(1 for r in sub if r['passed'])} | {len(sub)} |")
    fails = [r for r in scored if not r["passed"]]
    if fails:
        lines += ["", "## Failures"]
        for r in fails:
            lines += [f"- **{r['verdict']}** ({r['partition']}): {r['question']}",
                      f"  - bot said: {r['answer'][:300]}"]
            for c in r["unsupported_claims"]:
                lines.append(f"  - unsupported claim: {c}")
            if r["reasoning"]:
                lines.append(f"  - judge: {r['reasoning']}")
    errs = len(results) - len(scored)
    if errs:
        lines += ["", f"({errs} question(s) skipped: judge errors)"]

    md = "\n".join(lines)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", bot["name"].lower()).strip("-")
    (out_dir / f"{slug}-{run_tag}.md").write_text(md)
    (out_dir / f"{slug}-{run_tag}.json").write_text(json.dumps(
        {"bot": bot["name"], "bot_id": bot["id"], "run": run_tag,
         "judge": f"{provider}/{model}", "score": f"{passed}/{len(scored)}",
         "show_ready": show_ready, "results": results}, indent=2))
    print("\n" + md)
    print(f"\nSaved: {out_dir}/{slug}-{run_tag}.md (+ .json)")


if __name__ == "__main__":
    # chat_completion's budget counter touches the DB via Flask's `g`; a bare
    # app context is all it needs (create_app would start the scheduler).
    with Flask("eval_grounding").app_context():
        main()
