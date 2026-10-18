"""Stream B's rules gate for upstream tools, plugged into the core by `mulligan.services`."""

from __future__ import annotations

from mulligan.classify.rules import RulesClassifier
from mulligan.interfaces import Classifier


def create_classifier() -> Classifier:
    return RulesClassifier()
