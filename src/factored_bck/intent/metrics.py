"""Evaluation metrics in plain Python so notebooks and tests share one definition."""

import random
from math import comb

from factored_bck.intent.taxonomy import LABELS, PRIORITY


def gold_primary(labels):
    """Primary gold label under the accepted rule: loss or theft first."""
    return PRIORITY if PRIORITY in labels else labels[0]


def accuracy(gold, pred):
    return sum(g == p for g, p in zip(gold, pred, strict=True)) / len(gold)


def per_class(gold, pred, labels=LABELS):
    result = {}
    for label in labels:
        tp = sum(g == p == label for g, p in zip(gold, pred, strict=True))
        predicted = sum(p == label for p in pred)
        support = sum(g == label for g in gold)
        precision = tp / predicted if predicted else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        result[label] = {"precision": precision, "recall": recall, "f1": f1, "support": support}
    return result


def macro_f1(gold, pred, labels=LABELS):
    """Mean F1 over labels present in the gold set, so absent classes do not count as zero."""
    scores = per_class(gold, pred, labels)
    present = [label for label in labels if scores[label]["support"]]
    return sum(scores[label]["f1"] for label in present) / len(present)


def bootstrap_interval(metric, gold, pred, *, samples=2000, level=0.95, seed=0):
    """Percentile interval from resampling cases with replacement."""
    rng = random.Random(seed)
    n = len(gold)
    values = []
    for _ in range(samples):
        index = [rng.randrange(n) for _ in range(n)]
        values.append(metric([gold[i] for i in index], [pred[i] for i in index]))
    values.sort()
    tail = (1 - level) / 2
    return values[int(tail * samples)], values[int((1 - tail) * samples) - 1]


def mcnemar_p_value(correct_a, correct_b):
    """Exact two sided McNemar test on the cases where the two systems disagree."""
    only_a = sum(a and not b for a, b in zip(correct_a, correct_b, strict=True))
    only_b = sum(b and not a for a, b in zip(correct_a, correct_b, strict=True))
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(min(only_a, only_b) + 1)) / 2**n
    return min(1.0, 2 * tail)


def expected_calibration_error(confidences, correct, bins=10):
    """Weighted gap between stated confidence and observed accuracy per confidence bin."""
    total = 0.0
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        members = [
            i for i, c in enumerate(confidences) if low <= c < high or (b == bins - 1 and c == 1.0)
        ]
        if members:
            conf = sum(confidences[i] for i in members) / len(members)
            acc = sum(correct[i] for i in members) / len(members)
            total += len(members) / len(confidences) * abs(conf - acc)
    return total


def rule_of_three(n):
    """Approximate 95% upper bound on a rate after zero failures in n cases."""
    return 3 / n
