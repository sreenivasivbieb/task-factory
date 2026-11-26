"""The proposal state machine (v2.2 section 9).

Main path: DRAFTED -> CHECKED -> (PENDING_REVIEW) -> APPROVED -> BUILDING -> VALIDATED -> QUEUED
-> RUNNING -> BENCHMARKED. Every other edge is listed here with its event; anything not listed is
refused, so a bug cannot move a proposal somewhere the design does not allow.
"""
from __future__ import annotations

from ..errors import FactoryError
from .schema import ProposalState as S


class TransitionError(FactoryError):
    """A transition the state machine does not allow."""


TERMINAL = {S.REJECTED, S.MERGED, S.DROPPED}

# (from, event) -> to
TRANSITIONS: dict[tuple[S, str], S] = {
    (S.DRAFTED, "critics_pass"): S.CHECKED,
    (S.DRAFTED, "critic_objection"): S.REJECTED,
    (S.DRAFTED, "check_failed"): S.REJECTED,  # deterministic checks (bug fix, network, files)
    (S.DRAFTED, "near_duplicate"): S.MERGED,
    (S.CHECKED, "near_duplicate"): S.MERGED,
    (S.CHECKED, "auto_approve"): S.APPROVED,
    (S.CHECKED, "request_review"): S.PENDING_REVIEW,
    (S.PENDING_REVIEW, "user_approve"): S.APPROVED,
    (S.PENDING_REVIEW, "user_edit"): S.DRAFTED,
    (S.PENDING_REVIEW, "user_reject"): S.REJECTED,
    (S.APPROVED, "build"): S.BUILDING,
    (S.BUILDING, "repair"): S.BUILDING,
    (S.BUILDING, "validated"): S.VALIDATED,
    (S.BUILDING, "repairs_exhausted"): S.DROPPED,
    (S.VALIDATED, "enqueue"): S.QUEUED,
    (S.QUEUED, "lease"): S.RUNNING,
    (S.RUNNING, "lease"): S.RUNNING,  # a second job of the same task starts
    (S.RUNNING, "infra_retry"): S.QUEUED,
    (S.RUNNING, "finished"): S.BENCHMARKED,
    (S.RUNNING, "retries_exhausted"): S.RUN_FAILED,
    # benchmark another agent later
    (S.BENCHMARKED, "enqueue"): S.QUEUED,
    (S.RUN_FAILED, "enqueue"): S.QUEUED,
}

# "Any state: proposal or package edited" (checksum changes invalidate old results)
EDITABLE = {S.DRAFTED, S.CHECKED, S.PENDING_REVIEW, S.APPROVED, S.BUILDING, S.VALIDATED, S.QUEUED,
            S.RUNNING, S.BENCHMARKED, S.RUN_FAILED}
PACKAGE_EDITABLE = {S.VALIDATED, S.QUEUED, S.RUNNING, S.BENCHMARKED, S.RUN_FAILED}


def next_state(current: S | str, event: str) -> S:
    current = S(current)
    if event == "proposal_edited" and current in EDITABLE:
        return S.DRAFTED
    if event == "package_edited" and current in PACKAGE_EDITABLE:
        return S.QUEUED
    try:
        return TRANSITIONS[(current, event)]
    except KeyError:
        raise TransitionError(f"no transition from {current.value} on {event!r}") from None


def events_from(state: S | str) -> list[str]:
    state = S(state)
    out = sorted(e for (s, e) in TRANSITIONS if s == state)
    if state in EDITABLE:
        out.append("proposal_edited")
    if state in PACKAGE_EDITABLE:
        out.append("package_edited")
    return out
