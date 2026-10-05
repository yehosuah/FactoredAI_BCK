import pytest

from factored_bck.intent.baseline import predict_keyword
from factored_bck.intent.metrics import (
    accuracy,
    bootstrap_interval,
    choose_threshold,
    coverage_curve,
    expected_calibration_error,
    gold_primary,
    macro_f1,
    mcnemar_p_value,
    per_class,
    rule_of_three,
)


def test_gold_primary_prefers_loss_theft():
    assert gold_primary(("query_movements", "block_lost_stolen")) == "block_lost_stolen"
    assert gold_primary(("pause_card", "query_card_status")) == "pause_card"


def test_accuracy_and_macro_f1_on_a_small_example():
    gold = ["a", "a", "b", "b"]
    pred = ["a", "b", "b", "b"]
    assert accuracy(gold, pred) == 0.75
    # a: precision 1, recall 0.5, f1 2/3; b: precision 2/3, recall 1, f1 0.8
    assert macro_f1(gold, pred, labels=("a", "b")) == pytest.approx((2 / 3 + 0.8) / 2)
    assert per_class(gold, pred, labels=("a", "b"))["a"] == {
        "precision": 1.0,
        "recall": 0.5,
        "f1": pytest.approx(2 / 3),
        "support": 2,
    }


def test_bootstrap_interval_contains_the_point_estimate_and_is_reproducible():
    gold = ["a"] * 40 + ["b"] * 40
    pred = ["a"] * 30 + ["b"] * 10 + ["b"] * 40
    low, high = bootstrap_interval(accuracy, gold, pred, seed=7)
    assert low < accuracy(gold, pred) < high
    assert (low, high) == bootstrap_interval(accuracy, gold, pred, seed=7)


def test_mcnemar_uses_only_discordant_pairs():
    # 6 cases only A got right, 0 only B got right: exact two sided p = 2 * 0.5**6.
    a = [True] * 6 + [True] * 10 + [False] * 4
    b = [False] * 6 + [True] * 10 + [False] * 4
    assert mcnemar_p_value(a, b) == pytest.approx(2 * 0.5**6)
    assert mcnemar_p_value(a, a) == 1.0


def test_expected_calibration_error():
    assert expected_calibration_error([0.9, 0.9], [True, True]) == pytest.approx(0.1)
    # Separate bins: gaps 0.45 and 0.95, each weighted by half of the cases.
    assert expected_calibration_error([0.55, 0.95], [True, False]) == pytest.approx(0.7)
    assert expected_calibration_error([0.55, 0.95], [True, False], bins=2) == pytest.approx(0.25)


def test_rule_of_three():
    assert rule_of_three(50) == pytest.approx(0.06)


@pytest.mark.parametrize(
    "text,label",
    [
        ("Me robaron la tarjeta", "block_lost_stolen"),
        ("Perdi meu cartão e quero ver as compras", "block_lost_stolen"),
        ("Quiero pausar la tarjeta", "pause_card"),
        ("Não reconheço essa cobrança", "register_unrecognized_charge"),
        ("Quiero un préstamo", "unsupported_unclear"),
    ],
)
def test_keyword_baseline(text, label):
    assert predict_keyword(text) == label


def test_coverage_curve_and_threshold_choice():
    confidences = [0.95, 0.9, 0.8, 0.6, 0.4]
    correct = [True, True, True, False, False]
    curve = coverage_curve(confidences, correct, thresholds=(0.5, 0.7))
    assert curve == [
        {"threshold": 0.5, "coverage": 0.8, "accuracy": 0.75, "automated": 4},
        {"threshold": 0.7, "coverage": 0.6, "accuracy": 1.0, "automated": 3},
    ]
    assert choose_threshold(confidences, correct, target=0.9, thresholds=(0.5, 0.7)) == 0.7
    assert choose_threshold(confidences, correct, target=0.7, thresholds=(0.5, 0.7)) == 0.5
    assert choose_threshold([0.6], [False], target=0.9, thresholds=(0.5,)) is None
