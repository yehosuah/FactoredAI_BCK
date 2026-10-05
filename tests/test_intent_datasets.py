from collections import Counter

from factored_bck.intent.datasets import TRAIN_PATH, load_cases, normalize, overlap
from factored_bck.intent.taxonomy import LABELS


def test_training_cases_are_valid():
    cases = load_cases(TRAIN_PATH)
    assert len({case.example_id for case in cases}) == len(cases)
    assert {label for case in cases for label in case.labels} == set(LABELS)
    assert {case.language for case in cases} == {"es", "pt"}


def test_every_intent_has_enough_cases_per_language():
    counts = Counter((case.labels[0], case.language) for case in load_cases(TRAIN_PATH))
    assert min(counts[(label, lang)] for label in LABELS for lang in ("es", "pt")) >= 12


def test_no_duplicate_texts_after_normalization():
    texts = [normalize(case.text) for case in load_cases(TRAIN_PATH)]
    assert len(set(texts)) == len(texts)


def test_overlap_reports_shared_texts_and_families():
    cases = load_cases(TRAIN_PATH)
    assert overlap(cases[:3], cases[2:5]) == {"texts": 1, "families": 1}
    assert overlap(cases[:2], cases[3:5]) == {"texts": 0, "families": 0}
