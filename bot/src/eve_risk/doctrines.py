from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import median

from eve_risk.domain import DoctrineMatch, ShipTypeInfo


@dataclass(frozen=True)
class DoctrineRule:
    name: str
    core: frozenset[str]
    support: frozenset[str]
    minimum_core: float = 2.0


RULES: tuple[DoctrineRule, ...] = (
    DoctrineRule(
        "缪宁舰队",
        frozenset({"muninn"}),
        frozenset({"scimitar", "huginn", "loki", "stiletto", "sabre"}),
    ),
    DoctrineRule(
        "伊什塔舰队",
        frozenset({"ishtar"}),
        frozenset({"oneiros", "lachesis", "keres", "stiletto", "sabre"}),
    ),
    DoctrineRule(
        "猛鲑舰队",
        frozenset({"ferox", "ferox navy issue"}),
        frozenset({"basilisk", "vulture", "scorpion", "stiletto", "sabre"}),
    ),
    DoctrineRule(
        "飓风舰队",
        frozenset({"hurricane", "hurricane fleet issue"}),
        frozenset({"scimitar", "huginn", "claymore", "stiletto", "sabre"}),
    ),
    DoctrineRule(
        "重甲舰队",
        frozenset({"absolution", "damnation", "legion", "guardian"}),
        frozenset({"devoter", "curse", "lachesis", "proteus"}),
        minimum_core=3.0,
    ),
    DoctrineRule(
        "T3C 战略巡洋舰队",
        frozenset({"loki", "legion", "tengu", "proteus"}),
        frozenset({"guardian", "scimitar", "basilisk", "oneiros", "huginn"}),
        minimum_core=3.0,
    ),
    DoctrineRule(
        "高速游击队",
        frozenset({"vagabond", "cynabal", "orthrus", "garmur", "omen navy issue"}),
        frozenset({"stiletto", "sabre", "keres", "hyena", "scimitar"}),
        minimum_core=2.0,
    ),
    DoctrineRule(
        "隐轰队",
        frozenset({"hound", "manticore", "nemesis", "purifier"}),
        frozenset({"pacifier", "stiletto"}),
        minimum_core=3.0,
    ),
    DoctrineRule(
        "黑隐特勤舰队",
        frozenset({"redeemer", "panther", "sin", "widow", "marshal"}),
        frozenset({"loki", "proteus", "tengu", "legion", "rapier", "arazu"}),
        minimum_core=1.0,
    ),
)


def identify_doctrines(
    engagement_ship_counts: list[Counter[int]],
    ship_types: dict[int, ShipTypeInfo],
    weights: list[int] | None = None,
    limit: int = 3,
) -> list[DoctrineMatch]:
    if not engagement_ship_counts:
        return []
    weights = weights or [1] * len(engagement_ship_counts)
    if len(weights) != len(engagement_ship_counts):
        raise ValueError("Doctrine sample weights must match engagement samples")

    english_by_type: dict[int, str] = {}
    display_by_type: dict[int, str] = {}
    for type_id, info in ship_types.items():
        english_by_type[type_id] = (info.name_en or info.name).casefold()
        display_by_type[type_id] = info.name

    matches: list[DoctrineMatch] = []
    total_weight = sum(weights) or 1
    for rule in RULES:
        matched_samples: list[Counter[int]] = []
        matched_weights: list[int] = []
        qualities: list[float] = []
        support_type_names: set[str] = set()

        for sample, weight in zip(engagement_ship_counts, weights, strict=True):
            english_counts: Counter[str] = Counter()
            for type_id, count in sample.items():
                english = english_by_type.get(type_id)
                if english:
                    english_counts[english] += count
            core_count = sum(english_counts[name] for name in rule.core)
            if core_count < rule.minimum_core:
                continue
            support_names = {name for name in rule.support if english_counts[name] > 0}
            support_type_names.update(support_names)
            matched_count = core_count + sum(english_counts[name] for name in support_names)
            total_ships = sum(english_counts.values()) or 1
            qualities.extend([matched_count / total_ships] * weight)
            matched_samples.append(sample)
            matched_weights.append(weight)

        if len(matched_samples) < 2:
            continue

        matched_weight = sum(matched_weights)
        frequency = matched_weight / total_weight
        quality = median(qualities) if qualities else 0.0
        confidence = round(
            min(
                95,
                35
                + frequency * 20
                + quality * 20
                + min(10, len(support_type_names) * 3)
                + min(10, len(matched_samples) * 2),
            )
        )
        evidence = _typical_evidence(
            matched_samples,
            matched_weights,
            rule.core | rule.support,
            english_by_type,
            display_by_type,
        )
        matches.append(
            DoctrineMatch(
                name=rule.name,
                confidence=confidence,
                encounter_count=len(matched_samples),
                sample_count=len(engagement_ship_counts),
                evidence=evidence,
            )
        )

    return sorted(
        matches,
        key=lambda item: (item.confidence, item.encounter_count),
        reverse=True,
    )[:limit]


def _typical_evidence(
    samples: list[Counter[int]],
    weights: list[int],
    allowed_names: frozenset[str],
    english_by_type: dict[int, str],
    display_by_type: dict[int, str],
) -> list[str]:
    candidate_ids = {
        type_id
        for sample in samples
        for type_id in sample
        if english_by_type.get(type_id) in allowed_names
    }
    ranked: list[tuple[float, float, float, int]] = []
    for type_id in candidate_ids:
        values: list[float] = []
        present_weight = 0
        total_weight = 0
        for sample, weight in zip(samples, weights, strict=True):
            value = float(sample.get(type_id, 0))
            values.extend([value] * weight)
            total_weight += weight
            if value:
                present_weight += weight
        occurrence = present_weight / total_weight if total_weight else 0.0
        ranked.append((occurrence, median(values), _percentile(values, 0.75), type_id))

    ranked.sort(reverse=True)
    evidence: list[str] = []
    for occurrence, median_value, p75, type_id in ranked:
        if occurrence < 0.2:
            continue
        evidence.append(f"{display_by_type[type_id]} 通常{median_value:.0f} · 大场{p75:.0f}")
    return evidence[:4]


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    remainder = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * remainder
