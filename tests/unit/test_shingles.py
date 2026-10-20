from jobhunter.dedup.shingles import TextSide, containment, jaccard, shingles, text_confirms

WORDS = [f"wort{i}" for i in range(80)]


def varied(n: int, offset: int = 0) -> str:
    return " ".join(WORDS[(offset + i) % 80] for i in range(n))  # n distinct words: n - 4 distinct 5-shingles


MORE = [f"wort{i}" for i in range(300)]


def many(n: int, offset: int = 0) -> str:
    return " ".join(MORE[offset + i] for i in range(n))


def test_scores_and_g5_thresholds() -> None:
    assert (
        len(shingles("one two three four five six")) == 2
        and jaccard(shingles(varied(40)), shingles(varied(40))) == 1.0
    )
    # 56 and 26 distinct shingles, well over the 20-shingle floor
    full = TextSide(varied(60), "full")
    partial = TextSide(varied(30) + "…", "partial")
    assert text_confirms(full, full) == (True, 1.0) and text_confirms(partial, full)[0]  # 26/26 ≥ 0.7
    # disjoint shingle sets: Jaccard 0
    assert not text_confirms(TextSide(varied(30), "full"), TextSide(varied(30, 40), "full"))[0]
    # under 20 shingles: exact folded equality
    assert text_confirms(TextSide("short text", "full"), TextSide("short text", "full")) == (True, 1.0)
    assert not text_confirms(TextSide("short text", "full"), TextSide("short texts", "full"))[0]


def test_shingles_fold_their_words_and_measure_overlap() -> None:
    assert shingles("Über Straße, eins zwei drei") == shingles("uber strasse eins zwei drei")  # fold
    assert shingles("four words only here") == frozenset()  # fewer than five words: no 5-shingle
    small, big = shingles(many(30)), shingles(many(60))
    assert containment(small, big) == 1.0 and jaccard(small, big) == 26 / 56
    assert containment(frozenset(), big) == 0.0 and jaccard(frozenset(), frozenset()) == 0.0


def test_each_pair_kind_has_its_own_threshold() -> None:
    base = many(100)
    # full vs full: Jaccard ≥ 0.6 or containment ≥ 0.8; 75 shared words of 100: Jaccard 71/121, containment
    # 71/96; 90 shared words: Jaccard 86/106
    shifted = TextSide(many(100, 25), "full")
    ok, score = text_confirms(TextSide(base, "full"), shifted)
    assert not ok and round(score, 3) == round(71 / 96, 3)
    ok, score = text_confirms(TextSide(base, "full"), TextSide(many(100, 10), "full"))
    assert ok and round(score, 3) == round(86 / 96, 3)  # the better measure: containment
    # partial vs full: the partial's containment in the full ≥ 0.7; 80 words of which 60 in the full
    partial = TextSide(many(80, 40) + "…", "partial")
    ok, score = text_confirms(partial, TextSide(base, "full"))
    assert ok and round(score, 3) == round(56 / 76, 3)
    assert text_confirms(TextSide(base, "full"), partial) == (ok, score)  # either order
    # partial vs partial: containment ≥ 0.9; 56/76 is not enough
    assert not text_confirms(partial, TextSide(base + "…", "partial"))[0]
    assert text_confirms(TextSide(many(40) + "…", "partial"), TextSide(many(80) + "…", "partial")) == (
        True,
        1.0,
    )
    # the trailing ellipsis is removed before an exact comparison
    assert text_confirms(TextSide("short text…", "partial"), TextSide("Short text", "full")) == (True, 1.0)
