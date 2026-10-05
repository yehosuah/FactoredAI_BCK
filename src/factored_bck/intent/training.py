"""Training and export; needs the `ml` dependency group and never runs inside the API."""

import json
from pathlib import Path

from factored_bck.intent.model import char_ngrams
from factored_bck.intent.taxonomy import LABELS

SEED = 20261004


def _pipeline(c=10.0):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline

    return make_pipeline(
        TfidfVectorizer(analyzer=char_ngrams, sublinear_tf=True, min_df=2),
        LogisticRegression(C=c, max_iter=5000, random_state=SEED),
    )


def fit(cases, c=10.0):
    """Fit on the primary label of each case; every training case has exactly one."""
    pipeline = _pipeline(c)
    pipeline.fit([case.text for case in cases], [case.labels[0] for case in cases])
    return pipeline


def out_of_fold(cases, c=10.0, folds=5):
    """Probabilities for each case from a model that never saw its leakage family."""
    from sklearn.model_selection import StratifiedGroupKFold

    texts = [case.text for case in cases]
    labels = [case.labels[0] for case in cases]
    groups = [case.family_id for case in cases]
    result = [None] * len(cases)
    splitter = StratifiedGroupKFold(n_splits=folds, shuffle=True, random_state=SEED)
    for train, test in splitter.split(texts, labels, groups):
        pipeline = fit([cases[i] for i in train], c)
        classes = list(pipeline.classes_)
        for i, row in zip(test, pipeline.predict_proba([texts[i] for i in test]), strict=True):
            result[i] = {label: float(row[classes.index(label)]) for label in LABELS}
    return result


def export(pipeline, path, probe_cases, metadata):
    """Write weights in taxonomy order plus probe outputs that pin serving parity."""
    vectorizer, classifier = pipeline.steps[0][1], pipeline.steps[1][1]
    classes = list(classifier.classes_)
    if sorted(classes) != sorted(LABELS):
        raise ValueError("model classes do not match the taxonomy")
    order = [classes.index(label) for label in LABELS]
    payload = {
        "model_version": metadata.get("model_version", "intent-tfidf-lr-v1"),
        "labels": list(LABELS),
        "vocabulary": {gram: int(i) for gram, i in vectorizer.vocabulary_.items()},
        "idf": [float(x) for x in vectorizer.idf_],
        "coef": [[float(x) for x in classifier.coef_[i]] for i in order],
        "intercept": [float(classifier.intercept_[i]) for i in order],
        "metadata": metadata,
    }
    rows = pipeline.predict_proba([case.text for case in probe_cases])
    payload["probes"] = [
        {
            "text": case.text,
            "probabilities": {label: float(row[i]) for label, i in zip(LABELS, order, strict=True)},
        }
        for case, row in zip(probe_cases, rows, strict=True)
    ]
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), "utf-8")
    return path
