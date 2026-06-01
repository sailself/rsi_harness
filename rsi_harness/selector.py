from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CandidateScore:
    candidate_id: str
    hard_pass: bool
    test_pass_rate: float = 0.0
    generated_test_pass_rate: float = 0.0
    behavioral_vote_count: float = 1
    static_quality_score: float = 0.0
    risk_score: float = 0.0
    normalized_runtime: float = 0.0
    patch_size_penalty: float = 0.0
    output_bucket: str = ""
    score: float = 0.0


def score_candidate(candidate: CandidateScore) -> CandidateScore:
    score = (
        1000 * int(candidate.hard_pass)
        + 80 * candidate.test_pass_rate
        + 40 * candidate.generated_test_pass_rate
        + 30 * candidate.behavioral_vote_count
        + 20 * candidate.static_quality_score
        - 30 * candidate.risk_score
        - 10 * candidate.normalized_runtime
        - 5 * candidate.patch_size_penalty
    )
    return replace(candidate, score=score)


def select_winner(candidates: list[CandidateScore], selector: str = "score") -> CandidateScore:
    if not candidates:
        raise ValueError("Cannot select a winner without candidates")
    if selector == "behavioral_vote":
        candidates = _apply_behavioral_votes(candidates)
    scored = [score_candidate(candidate) for candidate in candidates]
    return sorted(scored, key=_selection_key)[0]


def _apply_behavioral_votes(candidates: list[CandidateScore]) -> list[CandidateScore]:
    """Self-consistency voting: a candidate's vote is the normalized share of
    candidates that produced the same executable behavior (output_bucket)."""
    total = len(candidates)
    cluster_sizes = Counter(candidate.output_bucket for candidate in candidates)
    return [
        replace(candidate, behavioral_vote_count=cluster_sizes[candidate.output_bucket] / total)
        for candidate in candidates
    ]


def _selection_key(candidate: CandidateScore) -> tuple:
    # Ascending sort; first element wins. Hard pass dominates via score; remaining
    # keys break exact ties on merit (more tests, more agreement, smaller/faster
    # patch) before falling back to candidate_id as a stable, non-biased tiebreaker.
    return (
        -candidate.score,
        not candidate.hard_pass,
        -candidate.test_pass_rate,
        -candidate.behavioral_vote_count,
        candidate.patch_size_penalty,
        candidate.normalized_runtime,
        candidate.candidate_id,
    )

