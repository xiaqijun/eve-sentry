"""Evidence helpers for threat scoring."""

from __future__ import annotations

from app.core.models import Evidence


def make_evidence(evidence_type: str, weight: int, summary: str) -> Evidence:
    """Create a normalized Evidence object."""
    return Evidence(
        evidence_type=evidence_type,
        weight=int(weight),
        summary=summary.strip(),
    )

