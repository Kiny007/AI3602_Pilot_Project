"""Artificial test data. NEVER a real Reddit corpus or model quality benchmark."""
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from privacy_pilot.common import write_jsonl
from privacy_pilot.config import DEFAULTS

EXAMPLES = {
    "I am 27 years old.": ("AGE", "self"),
    "I work as a nurse.": ("OCC", "self"),
    "I have a master's degree.": ("SCH", "self"),
    "I am married.": ("MAR", "self"),
    "I live in Boston.": ("LOC", "self"),
    "My annual income is 40000 dollars.": ("INC", "self"),
    "I have asthma.": ("HEA", "self"),
    "I have two children.": ("FAM", "family_relation"),
    "I am 180 cm tall.": ("APP", "self"),
    "I am pregnant.": ("SEX", "self"),
    "I was born in Madrid.": ("POB", "self"),
    "My name is Alex.": ("NAM", "self"),
}


def config(synthetic=True):
    cfg = deepcopy(DEFAULTS)
    cfg["source"].update(dataset="CONSTRUCTED-TEST-FIXTURE", version="fixture-v1", synthetic=synthetic, limit=1000)
    cfg["model"].update(name="scripted-test-transport", api_key_env="", retries=0)
    return cfg


def annotation(text):
    findings = []
    for phrase, (tag, subject) in EXAMPLES.items():
        if phrase in text:
            findings.append({"tag": tag, "evidence": phrase, "subject": subject,
                             "time": "past" if tag == "POB" else "present",
                             "assertion": "affirmative", "reason": "Constructed test oracle, not real inference"})
    return {"has_privacy": bool(findings), "review_required": False, "findings": findings}


def response(value):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def transport(request):
    text = json.loads(request["messages"][1]["content"])["text"]
    return response(annotation(text))


def raw_fixture(path):
    rows = [{"id": f"synthetic-{i}", "body": text, "synthetic": True} for i, text in enumerate(EXAMPLES)]
    rows += [{"id": "negative", "body": "The software release is ready.", "synthetic": True},
             {"id": "model-negative", "body": "I like the software release.", "synthetic": True},
             {"id": "duplicate", "body": "  I am 27 years old.  ", "synthetic": True},
             {"id": "removed", "body": "[removed]", "synthetic": True}]
    write_jsonl(path, rows)
