import json

import pytest

from factored_bck.intent.datasets import TRAIN_PATH, load_cases
from factored_bck.intent.model import MODEL_PATH, IntentModel, char_ngrams
from factored_bck.intent.taxonomy import LABELS


def test_char_ngrams_pad_each_word_and_keep_short_words_once():
    assert char_ngrams("Ok", 2, 3) == [" o", "ok", "k ", " ok", "ok "]
    assert char_ngrams("a", 2, 4) == [" a", "a ", " a "]


def test_packaged_model_reproduces_its_training_time_probabilities():
    model = IntentModel.load(MODEL_PATH)
    assert model.labels == LABELS
    for probe in model.probes:
        probabilities = model.predict_proba(probe["text"])
        assert sum(probabilities.values()) == pytest.approx(1)
        for label, expected in probe["probabilities"].items():
            assert probabilities[label] == pytest.approx(expected, abs=1e-6)


def test_export_matches_scikit_learn_on_every_training_text(tmp_path):
    pytest.importorskip("sklearn")
    from factored_bck.intent.training import export, fit

    cases = load_cases(TRAIN_PATH)
    pipeline = fit(cases)
    path = export(pipeline, tmp_path / "model.json", cases[:3], {"note": "test"})
    model = IntentModel.load(path)
    expected = pipeline.predict_proba([case.text for case in cases])
    classes = list(pipeline.classes_)
    for case, row in zip(cases, expected, strict=True):
        got = model.predict_proba(case.text)
        assert max(abs(got[label] - row[classes.index(label)]) for label in LABELS) < 1e-6


@pytest.mark.parametrize("corruption", ["labels", "nan", "infinity", "dimension", "indices"])
def test_malformed_local_weights_fail_before_model_serving(corruption):
    payload = json.loads(MODEL_PATH.read_text())
    if corruption == "labels":
        payload["labels"][0] = "confirm_card"
    elif corruption == "nan":
        payload["coef"][0][0] = float("nan")
    elif corruption == "infinity":
        payload["intercept"][0] = float("inf")
    elif corruption == "dimension":
        payload["idf"].pop()
    else:
        payload["vocabulary"][next(iter(payload["vocabulary"]))] = -1
    with pytest.raises(ValueError, match="invalid_intent_model"):
        IntentModel(payload)
