"""P7 optional review: proposal cards with approve / edit / reject (off by default).

An edit is logged as a manual edit with its reason and sends the proposal back to DRAFTED, so the
critics check it again. A rejection needs a reason. Nothing here can skip the gates or Harbor.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from ..errors import ConfigError
from . import drafting
from .schema import ProposalState as S, Review, TaskProposal
from .store import ReferenceStore

EDITABLE = ["title", "kind", "summary", "behaviour_spec", "expected_files", "test_plan", "difficulty_est"]


def card(p: TaskProposal) -> str:
    src = p.provenance[0] if p.provenance else None
    lines = [f"{p.proposal_id}  [{p.state.value}]  {p.title}",
             f"  kind: {p.kind}   category: {drafting.category(p)}   difficulty: {p.difficulty_est}"]
    if src:
        lines.append(f"  source: {src.peer_repo}  {src.pr_url}  licence {src.licence or 'unknown'}")
    nov = p.novelty
    lines.append(f"  novelty: {'not in repo' if not nov.in_repo else 'IN REPO'}, "
                 f"{'not upstream' if not nov.in_upstream else 'UPSTREAM'}"
                 + (f"  ({'; '.join(nov.evidence[:2])})" if nov.evidence else ""))
    lines.append(f"  summary: {p.summary}")
    lines.append(f"  expected files: {', '.join(p.expected_files) or '-'}")
    lines.append("  test plan:")
    lines += [f"    - {t}" for t in p.test_plan[:12]]
    crit = [r for r in p.reviews if r.reviewer.startswith(("critic:", "check:"))]
    if crit:
        lines.append("  critics:")
        lines += [f"    {r.reviewer}: {r.verdict}" + (f" - {'; '.join(r.objections)[:160]}" if r.objections else "")
                  for r in crit]
    if p.edits:
        lines.append(f"  manual edits: {len(p.edits)} ({'; '.join(e.reason for e in p.edits)[:160]})")
    return "\n".join(lines)


def approve(store: ReferenceStore, p: TaskProposal, user: str, note: str = "") -> TaskProposal:
    store.add_review(Review(proposal_id=p.proposal_id, reviewer=f"user:{user}", verdict="approve", reason=note))
    return store.transition(p.proposal_id, "user_approve", note or "approved by the reviewer", by=f"user:{user}",
                            approved_by=f"user:{user}")


def reject(store: ReferenceStore, p: TaskProposal, user: str, reason: str) -> TaskProposal:
    if not reason.strip():
        raise ConfigError("a reason is required to reject a proposal")
    store.add_review(Review(proposal_id=p.proposal_id, reviewer=f"user:{user}", verdict="reject", reason=reason))
    return store.transition(p.proposal_id, "user_reject", reason, by=f"user:{user}")


def edit(store: ReferenceStore, p: TaskProposal, user: str, changes: dict, reason: str) -> TaskProposal:
    store.add_review(Review(proposal_id=p.proposal_id, reviewer=f"user:{user}", verdict="edit", reason=reason))
    return store.edit_proposal(p.proposal_id, changes, by=f"user:{user}", reason=reason)


def edit_in_editor(p: TaskProposal) -> dict:
    """Open the editable fields as JSON in $EDITOR; returns the changed fields."""
    before = {k: getattr(p, k) for k in EDITABLE}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(before, f, indent=2, ensure_ascii=False)
        path = f.name
    try:
        subprocess.run([os.environ.get("EDITOR", "vi"), path], check=False)
        after = json.loads(Path(path).read_text(encoding="utf-8"))
    finally:
        Path(path).unlink(missing_ok=True)
    return {k: v for k, v in after.items() if k in EDITABLE and v != before.get(k)}


def interactive(store: ReferenceStore, run_id: str, user: str, ask=input, out=print) -> dict:
    """Walk the pending proposals: [a]pprove, [e]dit, [r]eject, [s]kip, [q]uit."""
    counts = {"approved": 0, "rejected": 0, "edited": 0, "skipped": 0}
    for p in store.list_proposals(run_id, [S.PENDING_REVIEW]):
        out("")
        out(card(p))
        while True:
            choice = (ask("  [a]pprove  [e]dit  [r]eject  [s]kip  [q]uit > ").strip().lower() or "s")[0]
            if choice == "a":
                approve(store, p, user, ask("  note (optional): ").strip())
                counts["approved"] += 1
                break
            if choice == "r":
                reason = ask("  reason: ").strip()
                if not reason:
                    out("  a reason is required")
                    continue
                reject(store, p, user, reason)
                counts["rejected"] += 1
                break
            if choice == "e":
                changes = edit_in_editor(p)
                if not changes:
                    out("  nothing changed")
                    continue
                reason = ask("  reason for the edit (disclosed in the report): ").strip()
                if not reason:
                    out("  a reason is required")
                    continue
                edit(store, p, user, changes, reason)
                counts["edited"] += 1
                break
            if choice == "s":
                counts["skipped"] += 1
                break
            if choice == "q":
                return counts
    return counts
