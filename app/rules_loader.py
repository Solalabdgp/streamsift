from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Keyword, RuleWeight
from app.scoring import RulesCache


async def load_rules_cache(version: int = 0) -> RulesCache:
    async with SessionLocal() as session:
        kw_rows = (await session.execute(select(Keyword).where(Keyword.enabled.is_(True)))).scalars().all()
        rw_rows = (await session.execute(select(RuleWeight))).scalars().all()

    cache = RulesCache(
        version=version,
        stack_cap=settings.stack_score_cap,
        hiring_cap=settings.hiring_score_cap,
        noise_cap=settings.noise_penalty_cap,
        notify_threshold=settings.notify_threshold,
        rules_version_tag=settings.rules_version,
    )
    for row in kw_rows:
        if row.dict_type in ("stack_core", "stack_periph"):
            cache.stack_weights[row.term] = row.weight
        elif row.dict_type in ("hiring_strong", "hiring_weak"):
            cache.hiring_weights[row.term] = row.weight
        elif row.dict_type == "money":
            cache.money_weights[row.term] = row.weight
        elif row.dict_type == "anti":
            cache.anti_terms.append(row.term)
        elif row.dict_type == "closed":
            cache.closed_terms.append(row.term)
        elif row.dict_type == "noise":
            cache.noise_weights[row.term] = row.weight
        elif row.dict_type == "alias" and row.maps_to:
            cache.aliases[row.term] = row.maps_to

    for row in rw_rows:
        if row.key == "__notify_threshold__":
            cache.notify_threshold = row.weight
        else:
            cache.rule_weights[row.key] = row.weight

    cache.compile()
    return cache
