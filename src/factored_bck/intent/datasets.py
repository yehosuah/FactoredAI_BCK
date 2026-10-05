"""Load intent cases from JSONL and check them for leakage between sets."""

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from factored_bck.intent.taxonomy import LABELS

ROOT = Path(__file__).resolve().parents[3]
TRAIN_PATH = ROOT / "ml" / "datasets" / "intent_train_v1.jsonl"


@dataclass(frozen=True)
class Case:
    example_id: str
    language: str
    text: str
    labels: tuple[str, ...]
    family_id: str


def normalize(text):
    """Lowercase, strip accents and collapse whitespace; also the model's input form."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip()


def _case(row):
    # Team training rows use `text`/`labels`; the ETL draft pack uses request fields.
    text = row.get("text") or row.get("request_text")
    labels = row.get("labels") or row.get("intent_labels") or [row.get("intent_label")]
    family = row.get("family_id") or (row.get("leakage_group_ids") or [row["example_id"]])[0]
    case = Case(row["example_id"], row["language"], text, tuple(labels), family)
    if case.language not in ("es", "pt") or not case.text or not case.labels:
        raise ValueError(f"invalid case {case.example_id}")
    if any(label not in LABELS for label in case.labels):
        raise ValueError(f"unknown label in {case.example_id}")
    return case


def load_cases(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [_case(json.loads(line)) for line in lines if line.strip()]


def overlap(first, second):
    """Count texts and leakage families shared by two sets of cases."""
    texts = {normalize(case.text) for case in first} & {normalize(case.text) for case in second}
    families = {case.family_id for case in first} & {case.family_id for case in second}
    return {"texts": len(texts), "families": len(families)}
