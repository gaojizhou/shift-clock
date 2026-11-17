"""Regional post name normalization (UI/selectable ↔ quota rule in 班制明细)."""

from __future__ import annotations

# UI / legacy selectable name → canonical name in quota_rule (班制明细)
REGION_POST_ALIASES: dict[str, dict[str, str]] = {
    "示例厂区B": {
        "物料转运工": "物料工",
    },
}


def quota_post(region: str, post: str) -> str:
    """Map selectable/UI post name to quota_rule.post for the region."""
    return REGION_POST_ALIASES.get(region, {}).get(post, post)


def scan_posts_for_quota(region: str, quota_post_name: str) -> list[str]:
    """All scan_record.post values that consume this quota slot."""
    names = [quota_post_name]
    for alias, canonical in REGION_POST_ALIASES.get(region, {}).items():
        if canonical == quota_post_name:
            names.append(alias)
        if alias == quota_post_name:
            names.append(canonical)
    return list(dict.fromkeys(names))
