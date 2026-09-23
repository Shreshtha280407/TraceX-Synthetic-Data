#!/usr/bin/env python3
"""EVALUATION-ONLY: applies ground-truth-driven review decisions to one
Fulcrum case's real entity-resolution candidates, via TraceX's real,
unmodified `POST /api/v1/entities/{entity_id}/resolution-review` endpoint.

NOT A PRODUCTION FEATURE. This module is not imported by, wired into, or
reachable from any part of TraceX itself -- it lives entirely in this
sibling repo and speaks to TraceX only through its real, public HTTP API,
exactly like a normal authenticated client would. It exists solely so
`graph/intelligence/evaluation.py` (TraceX, ADR-025) has real review
decisions to score against Fulcrum's dev/validation truth data -- see
TraceX's `docs/decisions/ADR-028-simulated-reviewer-evaluation-oracle.md`
for the full design writeup and its explicit non-production boundary.

WHY THIS EXISTS: `EntityReviewOutcome.VERIFIED_SAME` is "never automatic,
never inferred" by design (`app/modules/graph/entity_models.py`, TraceX) --
a real human review step gates every genuine same-identity assertion.
Without it, `evaluation.py`'s candidate_precision/candidate_recall/
false_link_rate are all trivially 0.0/null: the system has generated
candidates but nobody has ever judged any of them, so every candidate
implicitly reads as "predicted different" (see `evaluation.py::
_predicted_labels`). This tool stands in for that missing human step,
*only* for measuring retrieval/ranking quality on Fulcrum's own dev/val
splits -- it must never be pointed at Nightfall (the one true holdout,
ADR-016) or at any case a human might actually be investigating.

EXACT-MATCH ORACLE, NEVER GUESSED: a real candidate's `(left_entity_id,
right_entity_id)` pair is looked up, as an unordered pair, directly against
the ground-truth file's own `entity_pairs` -- the same unordered-pair-key
convention `evaluation.py::_pair_key` itself uses, so the oracle and the
scorer agree on pair identity by construction. A candidate whose exact pair
is not present in the truth file at all -- even if one of its two entities
appears in some *other* truth pair -- is left completely unreviewed: no
decision is submitted, and it is counted separately as "skipped, uncovered
by truth", never silently defaulted to either verdict.

Every submitted decision's `rationale` carries a fixed, greppable marker
(`SIMULATED_REVIEWER_RATIONALE` below) so the resulting `entity_review_
decisions` rows stay honestly distinguishable from genuine human judgment
in the database/audit trail, even though they were written through the
real endpoint by a real (if throwaway, evaluation-only) account.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from generate_entity_resolution_truth import get_json, post_json

ENTITY_CANDIDATES_PATH = "/api/v1/cases/{case_id}/entity-candidates"
RESOLUTION_REVIEW_PATH = "/api/v1/entities/{entity_id}/resolution-review"

#: Fixed marker stamped on every decision this tool submits -- greppable,
#: unmistakable, points back at the ADR that documents this tool's
#: existence and non-production status.
SIMULATED_REVIEWER_RATIONALE = (
    "[simulated-reviewer-oracle, evaluation-only] ground-truth-driven decision for "
    "offline evaluation; not a genuine human judgment -- see TraceX's docs/decisions/"
    "ADR-028-simulated-reviewer-evaluation-oracle.md"
)

#: Mirrors `EntityReviewOutcome`'s two decision-bearing values (TraceX,
#: `app/modules/graph/entity_models.py`). There is no `verified_different`
#: value in the real enum -- `evaluation.py::_predicted_labels` treats any
#: decision other than `verified_same` as the system's "different"
#: prediction, so `rejected` is the real-code equivalent of a truth-
#: confirmed "different" pair.
DECISION_FOR_LABEL = {"same": "verified_same", "different": "rejected"}


def _pair_key(left, right):
    """Order-independent identity for an (unordered) entity pair -- must
    match `evaluation.py::_pair_key` exactly so the oracle here and the
    real scorer never disagree about what counts as "the same pair"."""
    return frozenset({left, right})


def submit_review(api_url, token, entity_id, candidate_id, decision):
    query = urllib.parse.urlencode({"candidate_id": candidate_id})
    path = f"{RESOLUTION_REVIEW_PATH.format(entity_id=entity_id)}?{query}"
    return post_json(
        api_url, path, {"decision": decision, "rationale": SIMULATED_REVIEWER_RATIONALE}, token=token
    )


def apply_simulated_reviews(api_url, token, case_id, truth_path):
    """Apply ground-truth-driven review decisions to every real candidate
    TraceX has already generated for `case_id`, using `truth_path`'s
    `entity_pairs` as the sole oracle. Returns a `{outcome: count}` summary
    (`verified_same`/`rejected`/`skipped_uncovered`) -- printed by the
    caller so nothing is silently dropped from visibility.
    """
    truth = json.loads(Path(truth_path).read_text())
    oracle = {
        _pair_key(pair["left_entity_id"], pair["right_entity_id"]): pair["label"]
        for pair in truth["entity_pairs"]
    }

    response = get_json(api_url, ENTITY_CANDIDATES_PATH.format(case_id=case_id), token=token)
    counts = {"verified_same": 0, "rejected": 0, "skipped_uncovered": 0}
    for item in response["items"]:
        candidate = item["candidate"]
        left, right = candidate["left_entity_id"], candidate["right_entity_id"]
        label = oracle.get(_pair_key(left, right))
        if label is None:
            counts["skipped_uncovered"] += 1
            continue
        decision = DECISION_FOR_LABEL[label]
        submit_review(api_url, token, left, candidate["entity_resolution_candidate_id"], decision)
        counts[decision] += 1
    return counts
