from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CandidateScore:
    candidate_id: str
    hard_pass: bool
    test_pass_rate: float = 0.0
    generated_test_pass_rate: float = 0.0
    behavioral_vote_count: int = 1
    static_quality_score: float = 0.0
    risk_score: float = 0.0
    normalized_runtime: float = 0.0
    patch_size_penalty: float = 0.0
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


def select_winner(candidates: list[CandidateScore]) -> CandidateScore:
    if not candidates:
        raise ValueError("Cannot select a winner without candidates")
    scored = [score_candidate(candidate) for candidate in candidates]
    return sorted(scored, key=lambda item: (item.score, item.hard_pass, item.candidate_id), reverse=True)[0]

