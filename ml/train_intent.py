"""Train the shipped intent model: uv run --locked --group ml python ml/train_intent.py"""

import hashlib

import sklearn

from factored_bck.intent.datasets import TRAIN_PATH, load_cases
from factored_bck.intent.metrics import macro_f1
from factored_bck.intent.model import MODEL_PATH
from factored_bck.intent.taxonomy import LABELS, TAXONOMY_ID, TAXONOMY_VERSION
from factored_bck.intent.training import SEED, export, fit, out_of_fold

GRID = (0.3, 1.0, 3.0, 10.0, 30.0, 100.0, 300.0)


def main():
    cases = load_cases(TRAIN_PATH)
    gold = [case.labels[0] for case in cases]
    scores = {}
    for c in GRID:
        probabilities = out_of_fold(cases, c)
        pred = [max(p, key=p.get) for p in probabilities]
        scores[c] = macro_f1(gold, pred, LABELS)
        print(f"C={c:<5} out-of-fold macro F1 = {scores[c]:.3f}")
    best = max(GRID, key=lambda c: (scores[c], -c))
    metadata = {
        "model_version": "intent-tfidf-lr-v1",
        "taxonomy": f"{TAXONOMY_ID}@{TAXONOMY_VERSION}",
        "training_file": TRAIN_PATH.name,
        "training_sha256": hashlib.sha256(TRAIN_PATH.read_bytes()).hexdigest(),
        "training_cases": len(cases),
        "c": best,
        "c_grid_out_of_fold_macro_f1": {str(c): round(s, 4) for c, s in scores.items()},
        "seed": SEED,
        "scikit_learn": sklearn.__version__,
    }
    export(fit(cases, best), MODEL_PATH, cases[::28], metadata)
    print(f"chosen C={best}; wrote {MODEL_PATH.name}")


if __name__ == "__main__":
    main()
