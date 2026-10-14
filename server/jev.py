"""Jev (TypeSafe System One) client for every cron/script in this repo.

Jev is not an LLM and this is not ``llm.py``. It generates no text. You hand
it a blob of state plus questions whose answers are declared up front, and it
returns typed answers with probabilities. Use it for the judgement calls the
fleet already makes thousands of times a day where the answer comes from a
fixed list: is this spam, which bucket, how bad, is it worth flagging.

One entry point: ``decide(state, questions) -> Decision | None``. It returns a
``Decision`` on success and **None on ANY failure** (not configured, switched
off, over cap, network error, HTTP error, malformed response). Callers keep
their existing deterministic behaviour as the fallback path, exactly like
``llm.py``. This module never raises out of ``decide()``.

Questions are built with the three primitives::

    jev.noul("The sender is trying to sell us something")
    jev.choice("Which team owns this", {"billing": "...", "tech": "..."})
    jev.score("How urgent", ["can wait", "this week", "today"])

All questions in one call are billed against one copy of the state and answered
independently, so ask everything you might need at once (the "speculative
fan-out" pattern) and ignore the answers you don't use. Adding a question is
nearly free; a second call is not.

Reading answers::

    d = jev.decide(state, {"spam": jev.noul("..."), "team": jev.choice(...)})
    if d is None:            # fall back to the deterministic path
        ...
    d.noul("spam")           # 0.0 - 1.0
    d.choice("team")         # winning label
    d.confidence("team")     # 0.0 - 1.0
    d.probabilities("team")  # {label: p}
    d.score("urgency")       # float, may land between rubric levels

Config (all read at call time so cron env decides):

- ``TYPESAFE_API_KEY``  : required; without it ``available()`` is False.
- ``JEV_ENABLED``       : default true; set false to kill every Jev call
  fleet-wide. ``decide()`` returns None immediately, no network.
- ``JEV_DAILY_CAP``     : default 0 (unlimited). Max calls per UTC day shared
  by every script importing this module, enforced by the same atomic
  day-keyed counter file pattern ``llm.py`` uses. Fails open.
- ``JEV_MODEL``         : overrides the pinned default.
- ``JEV_LOG``           : JSONL calibration log path; default
  ``data/state/jev_calls.jsonl``. Set to "" to disable.
- ``TYPESAFE_BASE_URL`` : default https://api.typesafe.ai

**Pin the model.** ``DEFAULT_MODEL`` is an exact version, not ``jev-latest``.
Every caller gates on a threshold, and a silent model bump moves the
thresholds under them. Upgrades are a deliberate edit plus a recalibration
pass over the log, not something that happens overnight.

**Log the distribution, not just the answer.** Every call appends one JSONL
line with the full probabilities, confidence, latency and the caller's
``tag``. That file is the only way to pick honest thresholds later, and it is
how you tell "the model got worse" from "my threshold was always wrong".

What Jev cannot do, so don't ask: write text, count things, or do date or
money arithmetic. Compute those in code and put the result in the state.
It also reads instructions literally, so questions want to be blunt, atomic
and free of implied conditions.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "https://api.typesafe.ai"
SYSTEM_ONE_PATH = "/v1/systemone"
# Pinned on purpose. See the module docstring before changing it.
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_TIMEOUT = 20
# $ per input token. Output tokens are free.
INPUT_COST_PER_TOKEN = 0.042 / 1_000_000

_FALSE_VALUES = {"0", "false", "no", "off"}
_cap_warned = False


def _state_dir() -> str:
    """``<repo>/data/state``, found whether this file sits at the repo root or
    one package down (``common/jev.py``). Keeping one file that works in both
    layouts is what lets every repo hold a byte-identical copy."""
    override = os.environ.get("JEV_STATE_DIR")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    for root in (here, os.path.dirname(here)):
        if os.path.isdir(os.path.join(root, "data")):
            return os.path.join(root, "data", "state")
    return os.path.join(here, "data", "state")


def _app_tag() -> str:
    """The repo this copy lives in, used as the default log tag so one shared
    calibration log stays readable when several services write to it."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here) if os.path.basename(here) in (
        "common", "core", "server", "engine", "lib") else here
    return os.path.basename(root) or "jev"


APP_TAG = _app_tag()
_DEFAULT_COUNTER_FILE = os.path.join(_state_dir(), "jev_daily_calls.json")
_DEFAULT_LOG_FILE = os.path.join(_state_dir(), "jev_calls.jsonl")


# --- Question builders -------------------------------------------------------

def noul(instructions, true=None, false=None):
    """A yes/no proposition. The answer is a probability, not a boolean.

    Phrase it as a statement that is either true or false of the state
    ("The customer is asking for a refund"), not as a question.
    """
    q = {"type": "noul", "instructions": instructions}
    if true is not None or false is not None:
        q["criteria"] = {"true": true, "false": false}
    return q


def choice(instructions, criteria):
    """Pick one label. ``criteria`` maps label -> description (255 max).

    Descriptions do the work; the labels are just keys your code switches on.
    """
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions, criteria):
    """An ordered rubric, 2 to 10 levels, lowest first.

    The answer can land between levels (1.7 means "past adequate, not strong"),
    so compare it against a threshold rather than rounding it to a level.
    """
    return {"type": "score", "instructions": instructions, "criteria": list(criteria)}


# --- Governor ----------------------------------------------------------------

def enabled() -> bool:
    """False only when JEV_ENABLED is explicitly falsey (0/false/no/off)."""
    v = os.environ.get("JEV_ENABLED")
    if v is None:
        return True
    return v.strip().lower() not in _FALSE_VALUES


def available() -> bool:
    """True when Jev is switched on and a key is present. Still no promise of
    success (cap, network, account), so keep the deterministic fallback."""
    return enabled() and bool(os.environ.get("TYPESAFE_API_KEY"))


def _daily_cap() -> int:
    v = os.environ.get("JEV_DAILY_CAP")
    if not v:
        return 0
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _reserve_call(cap: int) -> bool:
    """Atomically take one slot from today's budget. Fails OPEN on any I/O
    error so a broken state file never silences the fleet."""
    import fcntl

    path = os.environ.get("JEV_COUNTER_FILE") or _DEFAULT_COUNTER_FILE
    today = _utc_today()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read()
                data = json.loads(raw) if raw.strip() else {}
                if not isinstance(data, dict):
                    data = {}
            except (ValueError, OSError):
                data = {}
            if data.get("date") != today:
                data = {"date": today, "count": 0}
            if int(data.get("count", 0)) >= cap:
                return False
            data["count"] = int(data.get("count", 0)) + 1
            f.seek(0)
            f.truncate()
            f.write(json.dumps(data))
            f.flush()
            return True
    except Exception as e:  # noqa: BLE001, never raise out of decide()
        print(f"[WARN] Jev daily-cap counter unavailable ({type(e).__name__}: {e})"
              f", allowing the call", file=sys.stderr)
        return True


def _governor_allows() -> bool:
    global _cap_warned
    cap = _daily_cap()
    if cap <= 0:
        return True
    if _reserve_call(cap):
        return True
    if not _cap_warned:
        print(f"[WARN] Jev daily cap of {cap} call(s) reached for {_utc_today()} "
              f"(UTC); further calls return None until the date rolls",
              file=sys.stderr)
        _cap_warned = True
    return False


# --- Decision ----------------------------------------------------------------

class Decision:
    """Typed answers from one call, plus what the call cost.

    Accessors return None for a question that was not asked or came back in an
    unexpected shape, so a caller that checks ``is None`` once at the top does
    not need to defend every field.
    """

    __slots__ = ("answers", "model", "input_tokens", "output_tokens", "ms")

    def __init__(self, answers, model, input_tokens, output_tokens, ms):
        self.answers = answers
        self.model = model
        self.input_tokens = input_tokens or 0
        self.output_tokens = output_tokens or 0
        self.ms = ms

    def _get(self, name, key):
        a = self.answers.get(name)
        if not isinstance(a, dict):
            return None
        v = a.get(key)
        return v

    def noul(self, name, default=None):
        """Probability that the proposition is true, 0.0 - 1.0."""
        v = self._get(name, "noul")
        return default if v is None else float(v)

    def choice(self, name, default=None):
        """The winning label."""
        v = self._get(name, "choice")
        return default if v is None else v

    def score(self, name, default=None):
        """Expected score; may land between rubric levels."""
        v = self._get(name, "score")
        return default if v is None else float(v)

    def confidence(self, name, default=None):
        """How concentrated the distribution is (choice and score only).

        Not a correctness guarantee. A confident answer is still wrong
        sometimes; that is what the middle band of ``gate()`` is for.
        """
        v = self._get(name, "confidence")
        return default if v is None else float(v)

    def probabilities(self, name):
        v = self._get(name, "probabilities")
        return v if isinstance(v, dict) else {}

    @property
    def cost_usd(self) -> float:
        return self.input_tokens * INPUT_COST_PER_TOKEN

    def __repr__(self):
        return (f"<Decision {self.model} {len(self.answers)} answer(s) "
                f"{self.ms}ms ${self.cost_usd:.6f}>")


def gate(value, act_above, ignore_below):
    """Three-way routing off one probability or score.

    Returns ``"act"`` above the high threshold, ``"ignore"`` below the low one,
    and ``"review"`` in between. The middle band is the point: it is where a
    human or a slower model looks, and how wide you make it is the only real
    safety dial you have. ``None`` in gives ``"review"``, so a failed call
    escalates instead of silently passing.
    """
    if value is None:
        return "review"
    if value >= act_above:
        return "act"
    if value < ignore_below:
        return "ignore"
    return "review"


# --- The call ----------------------------------------------------------------

def _log(tag, questions, decision, state_keys):
    path = os.environ.get("JEV_LOG", _DEFAULT_LOG_FILE)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tag": tag,
            "model": decision.model,
            "ms": decision.ms,
            "input_tokens": decision.input_tokens,
            "state_keys": state_keys,
            "answers": decision.answers,
        }
        with open(path, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:  # noqa: BLE001, logging must never break a decision
        pass


def decide(state, questions, *, model=None, tag=APP_TAG, timeout=DEFAULT_TIMEOUT):
    """Ask Jev one or more questions about ``state``. None on any failure.

    ``state`` is anything JSON-serialisable: a dict is usual, a string is fine.
    Send what an expert would need to answer and nothing else, because accuracy drops
    as the state fills with material the questions don't use.

    ``questions`` maps your own answer names to the builders above.
    """
    if not enabled():
        return None
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        return None
    if not questions:
        return None
    if not _governor_allows():
        return None

    base = (os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    body = {
        "state": state,
        "model": model or os.environ.get("JEV_MODEL") or DEFAULT_MODEL,
        "questions": questions,
    }
    req = Request(
        base + SYSTEM_ONE_PATH,
        data=json.dumps(body, default=str).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"{APP_TAG}-jev/1.0",
        },
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        print(f"[WARN] Jev HTTP {e.code}: {detail}", file=sys.stderr)
        return None
    except (URLError, TimeoutError, ValueError, OSError) as e:
        print(f"[WARN] Jev call failed ({type(e).__name__}: {e})", file=sys.stderr)
        return None

    answers = payload.get("answers")
    if not isinstance(answers, dict):
        print("[WARN] Jev response had no answers", file=sys.stderr)
        return None
    usage = payload.get("usage") or {}
    d = Decision(
        answers=answers,
        model=payload.get("model", ""),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        ms=round((time.monotonic() - t0) * 1000),
    )
    _log(tag, questions, d, sorted(state.keys()) if isinstance(state, dict) else None)
    return d
