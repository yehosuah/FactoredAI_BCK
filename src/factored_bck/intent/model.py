"""Trained intent classifier served from exported JSON weights, without scikit-learn."""

import json
from collections import Counter
from math import exp, isfinite, log, sqrt
from pathlib import Path

from factored_bck.intent.datasets import normalize
from factored_bck.intent.taxonomy import LABELS

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
        self._validate(payload)
        self.version = payload["model_version"]
        self.labels = tuple(payload["labels"])
        self.vocabulary = payload["vocabulary"]
        self.idf = payload["idf"]
        self.coef = payload["coef"]
        self.intercept = payload["intercept"]
        self.probes = payload.get("probes", [])
        self.metadata = payload.get("metadata", {})

    @staticmethod
    def _validate(payload):
        """Reject malformed local artifacts at startup, before proposing any tools."""
        if not isinstance(payload, dict):
            raise ValueError("invalid_intent_model")
        vocabulary = payload.get("vocabulary")
        if not isinstance(vocabulary, dict) or not 1 <= len(vocabulary) <= 100_000:
            raise ValueError("invalid_intent_model")
        size = len(vocabulary)
        if (
            payload.get("labels") != list(LABELS)
            or not isinstance(payload.get("model_version"), str)
            or not 1 <= len(payload["model_version"]) <= 100
            or any(
                not isinstance(gram, str) or not 2 <= len(gram) <= 4 or type(index) is not int
                for gram, index in vocabulary.items()
            )
            or set(vocabulary.values()) != set(range(size))
        ):
            raise ValueError("invalid_intent_model")

        def vector(value, length, positive=False):
            return (
                isinstance(value, list)
                and len(value) == length
                and all(
                    type(number) in (int, float)
                    and abs(number) <= 1_000_000
                    and isfinite(number)
                    and (not positive or number > 0)
                    for number in value
                )
            )

        coefficients = payload.get("coef")
        if (
            not vector(payload.get("idf"), size, positive=True)
            or not vector(payload.get("intercept"), len(LABELS))
            or not isinstance(coefficients, list)
            or len(coefficients) != len(LABELS)
            or any(not vector(row, size) for row in coefficients)
        ):
            raise ValueError("invalid_intent_model")

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
