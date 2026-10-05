"""Trained intent classifier served from exported JSON weights, without scikit-learn."""

import json
from collections import Counter
from math import exp, log, sqrt
from pathlib import Path

from factored_bck.intent.datasets import normalize

MODEL_PATH = Path(__file__).with_name("intent_model_v1.json")
NGRAM_RANGE = (2, 4)


def char_ngrams(text, low=NGRAM_RANGE[0], high=NGRAM_RANGE[1]):
    """Character n-grams inside space padded words, as scikit-learn's char_wb analyzer."""
    grams = []
    for word in normalize(text).split():
        padded = f" {word} "
        for n in range(low, high + 1):
            offset = 0
            grams.append(padded[:n])
            while offset + n < len(padded):
                offset += 1
                grams.append(padded[offset : offset + n])
            if offset == 0:
                break
    return grams


class IntentModel:
    """Sublinear TF-IDF with L2 norm, then multinomial logistic regression."""

    def __init__(self, payload):
        self.version = payload["model_version"]
        self.labels = tuple(payload["labels"])
        self.vocabulary = payload["vocabulary"]
        self.idf = payload["idf"]
        self.coef = payload["coef"]
        self.intercept = payload["intercept"]
        self.probes = payload.get("probes", [])
        self.metadata = payload.get("metadata", {})

    @classmethod
    def load(cls, path=MODEL_PATH):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def _vector(self, text):
        counts = Counter(self.vocabulary[g] for g in char_ngrams(text) if g in self.vocabulary)
        weights = {i: (1 + log(tf)) * self.idf[i] for i, tf in counts.items()}
        norm = sqrt(sum(w * w for w in weights.values()))
        return {i: w / norm for i, w in weights.items()} if norm else {}

    def predict_proba(self, text):
        vector = self._vector(text)
        scores = [
            b + sum(row[i] * w for i, w in vector.items())
            for row, b in zip(self.coef, self.intercept, strict=True)
        ]
        top = max(scores)
        exps = [exp(s - top) for s in scores]
        total = sum(exps)
        return {label: e / total for label, e in zip(self.labels, exps, strict=True)}
