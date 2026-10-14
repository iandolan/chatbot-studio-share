"""House style: no em-dashes or double hyphens in anything a visitor reads."""
import pytest

from server.textfmt import DashStripper, strip_dashes

EM = "—"
EN = "–"


@pytest.mark.parametrize("raw,want", [
    (f"built for law firms {EM} not a generic playbook",
     "built for law firms, not a generic playbook"),
    (f"low cost per case{EM}from day one", "low cost per case, from day one"),
    (f"Monday {EN} Friday, 9 AM {EN} 6 PM CST", "Monday to Friday, 9 AM to 6 PM CST"),
    (f"Monday{EN}Friday, 9 AM{EN}6 PM CST", "Monday to Friday, 9 AM to 6 PM CST"),
    (f"open 9:30 AM{EN}5 PM", "open 9:30 AM to 5 PM"),
    (f"{EN}2019{EN}2021 was the window", "2019 to 2021 was the window"),
    ("we fix it -- then we measure", "we fix it, then we measure"),
    (f"one {EM} two {EM} three", "one, two, three"),
])
def test_dashes_become_house_punctuation(raw, want):
    assert strip_dashes(raw) == want


def test_text_without_dashes_is_returned_unchanged():
    clean = "Nothing to do here, so this should come back identical."
    assert strip_dashes(clean) is clean


def test_is_idempotent():
    once = strip_dashes(f"a {EM} b {EN} c")
    assert strip_dashes(once) == once


def test_handles_empty_and_none():
    assert strip_dashes("") == ""
    assert strip_dashes(None) is None


class TestStreaming:
    """A dash and its surrounding space often land in different chunks."""

    def _run(self, chunks):
        s = DashStripper()
        return "".join(s.feed(c) for c in chunks) + s.flush()

    def test_dash_split_across_chunks_does_not_leave_a_floating_comma(self):
        # Naive per-chunk normalising gives "the firm , and so on".
        assert self._run(["the firm ", f"{EM} and so on"]) == "the firm, and so on"

    def test_dash_alone_in_its_own_chunk(self):
        assert self._run(["alpha ", EM, " beta"]) == "alpha, beta"

    def test_streamed_output_matches_one_shot_when_there_are_no_ranges(self):
        text = f"We serve law firms {EM} only law firms {EN} nationwide."
        for n in range(1, 25):
            chunks = [text[i:i + n] for i in range(0, len(text), n)]
            assert self._run(chunks) == strip_dashes(text), f"chunk size {n}"

    def test_no_dash_survives_any_chunking(self):
        """The guarantee that matters: a visitor never sees one.

        Range wording ("9 AM to 6 PM") needs context on both sides of the dash
        and is applied to the stored reply, not mid-stream, so it is not
        asserted here.
        """
        import random
        texts = [
            f"We serve law firms {EM} only law firms {EN} nationwide, 9 AM {EN} 6 PM.",
            f"Office hours Monday{EN}Friday. Founded 2019{EN}2021 {EM} still going.",
            f"{EM}leading dash and a trailing one{EM}",
            f"a{EM}b{EN}c{EM}d",
            "no dashes here at all, just prose.",
        ]
        rng = random.Random(0)
        for text in texts:
            for n in range(1, 25):
                out = self._run([text[i:i + n] for i in range(0, len(text), n)])
                assert EM not in out and EN not in out, f"{text!r} at chunk size {n}"
            for _ in range(50):
                chunks, i = [], 0
                while i < len(text):
                    k = rng.randint(1, 9)
                    chunks.append(text[i:i + k])
                    i += k
                out = self._run(chunks)
                assert EM not in out and EN not in out, f"ragged: {text!r}"

    def test_streaming_preserves_every_non_dash_character(self):
        text = f"Alpha {EM} bravo {EN} charlie delta."
        for n in range(1, 20):
            out = self._run([text[i:i + n] for i in range(0, len(text), n)])
            for word in ("Alpha", "bravo", "charlie", "delta"):
                assert word in out, f"lost {word!r} at chunk size {n}"

    def test_clean_stream_passes_through_intact(self):
        chunks = ["Plain ", "text with ", "no dashes at all."]
        assert self._run(chunks) == "Plain text with no dashes at all."

    def test_flush_emits_the_held_back_tail_and_then_nothing(self):
        s = DashStripper()
        # The trailing space is held back, in case a dash follows it.
        assert s.feed("trailing ") == "trailing"
        assert s.flush() == " "
        assert s.flush() == ""

    def test_nothing_is_lost_when_the_text_ends_on_whitespace(self):
        assert self._run(["trailing "]) == "trailing "
