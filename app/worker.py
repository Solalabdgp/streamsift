"""
Worker: потребляет queue:raw из Redis Stream, прогоняет пайплайн
нормализация -> дедуп -> жёсткие фильтры -> скоринг -> matches,
кладёт результат в Postgres и решает notify/digest/suppress.
"""
import asyncio
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.dedup import (
    exact_duplicate_of,
    hamming_distance,
    register_exact,
    register_simhash,
    simhash64,
    simhash_candidates,
    text_hash,
    to_signed64,
)
from app.filters import run_hard_filters
from app.ml import classify as ml_classify
from app.models import Author, Match, RawMessage, Source
from app.normalize import apply_aliases, compute_struct_features, extract_headline, normalize_text
from app.redis_client import (
    DIGEST_QUEUE,
    KEY_RELOAD_SIGNAL,
    NOTIFY_QUEUE,
    STREAM_GROUP,
    STREAM_RAW,
    get_redis,
)
from app.rules_loader import load_rules_cache
from app.scoring import score_message
from app.source_cache import SourceCache

log = structlog.get_logger("worker")

CONSUMER_NAME = "worker-1"


def _in_quiet_hours(now_utc: datetime) -> bool:
    local = now_utc.astimezone(ZoneInfo(settings.tz))
    start, end = settings.quiet_start, settings.quiet_end
    t = local.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # диапазон через полночь


async def _get_or_create_author_bonus(session, sender_id: int | None, rules) -> int:
    if sender_id is None:
        return 0
    row = (await session.execute(select(Author).where(Author.sender_id == sender_id))).scalar_one_or_none()
    if row is None:
        return 0
    bonus_good = rules.rule_weights.get("author_reputation_bonus", 2)
    if row.good_count >= 3:
        return bonus_good
    if row.bad_count >= 5 and row.good_count == 0:
        return -5
    return 0


async def _ensure_source_stub(session, payload: dict) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = pg_insert(Source).values(
        id=payload["source_id"],
        title=payload.get("source_title") or str(payload["source_id"]),
        username=payload.get("source_username"),
        kind=payload.get("source_kind_guess") or "other",
        is_channel=bool(payload.get("source_is_channel")),
        source_is_group=bool(payload.get("source_is_group")),
    ).on_conflict_do_nothing(index_elements=[Source.id])
    await session.execute(stmt)


async def process_message(payload: dict, rules, source_cache: SourceCache, redis) -> None:
    source_id = payload["source_id"]

    async with SessionLocal() as session:
        source = await source_cache.get(source_id)
        if source is None:
            await _ensure_source_stub(session, payload)
            await session.commit()
            source = await source_cache.get(source_id)  # может быть None ещё 60с — ок, обработаем как enabled=True по умолчанию

        if source is not None:
            if not source.enabled:
                return
            if source.muted_until is not None and source.muted_until > datetime.now(timezone.utc):
                return

        raw_text = payload["text"]
        struct = compute_struct_features(raw_text)
        text_norm_base = normalize_text(raw_text)
        text_norm = apply_aliases(text_norm_base, rules.alias_re, rules.aliases)

        h = text_hash(text_norm)
        s64 = simhash64(text_norm)

        posted_at = datetime.fromisoformat(payload["posted_at"])

        dup_of = await exact_duplicate_of(redis, h)
        if dup_of is None:
            candidates = await simhash_candidates(redis, s64)
            if candidates:
                rows = (
                    await session.execute(
                        select(RawMessage.id, RawMessage.simhash).where(RawMessage.id.in_(candidates))
                    )
                ).all()
                for cand_id, cand_simhash in rows:
                    if hamming_distance(s64, cand_simhash) <= 3:
                        dup_of = cand_id
                        break

        # Ingest adapter отдаёт готовый permalink на исходный пост — worker его не конструирует.
        link = payload.get("permalink")

        reject_rule = "duplicate" if dup_of is not None else None
        stack_matched: list[str] = []
        score_result = None

        if reject_rule is None:
            from app.scoring import _matched_terms  # локальный импорт, чтобы не тянуть в публичный API

            stack_matched = _matched_terms(rules.stack_re, text_norm)
            reject_rule = run_hard_filters(
                text_norm=text_norm,
                stack_matched=stack_matched,
                reply_to_id=payload.get("reply_to_id"),
                closed_re=rules.closed_re,
                anti_re=rules.anti_re,
                hiring_re=rules.hiring_re,
            )

        source_kind = source.kind if source else (payload.get("source_kind_guess") or "other")

        if reject_rule is None:
            hiring_matched_probe = rules.hiring_re.search(text_norm) if rules.hiring_re else None
            if not hiring_matched_probe and source_kind != "job_board":
                reject_rule = "no_hiring_signal"

        raw = RawMessage(
            source_id=source_id,
            message_id=payload["message_id"],
            thread_id=payload.get("thread_id"),
            sender_id=payload.get("sender_id"),
            reply_to_id=payload.get("reply_to_id"),
            posted_at=posted_at,
            text=raw_text,
            text_norm=text_norm,
            text_hash=h,
            simhash=to_signed64(s64),
            link=link,
            is_forward=bool(payload.get("is_forward")),
            struct_features=struct,
            reject_rule=reject_rule,
        )
        session.add(raw)
        await session.flush()  # получить raw.id

        await register_exact(redis, h, raw.id)
        await register_simhash(redis, s64, raw.id)

        if dup_of is not None:
            orig_match = (
                await session.execute(select(Match).where(Match.raw_message_id == dup_of))
            ).scalar_one_or_none()
            if orig_match is not None:
                orig_match.duplicate_count += 1
                await redis.rpush("queue:dup_notify", orig_match.id)
            await session.commit()
            return

        if reject_rule is not None:
            await session.commit()
            return

        author_bonus = await _get_or_create_author_bonus(session, payload.get("sender_id"), rules)
        priority = source.priority if source else 0

        result = score_message(
            text_norm=text_norm,
            struct_features=struct,
            reply_to_id=payload.get("reply_to_id"),
            source_kind=source_kind,
            source_priority=priority,
            author_reputation_bonus=author_bonus,
            rules=rules,
        )

        decided_by = "rules"
        model_version = None
        ml_prob = None
        final_score = result.score

        if settings.ml_enabled and settings.grey_low <= result.score < settings.grey_high:
            ml_result = ml_classify(text_norm)
            if ml_result is not None:
                ml_prob, model_version = ml_result
                decided_by = "ml"
                will_notify = ml_prob >= settings.ml_threshold
            else:
                will_notify = result.score >= rules.notify_threshold
        else:
            will_notify = result.score >= rules.notify_threshold

        headline = extract_headline(raw_text)
        status = "new" if will_notify else "suppressed"

        match = Match(
            raw_message_id=raw.id,
            score=final_score,
            signals=result.signals,
            stack_matched=result.stack_matched,
            compensation=result.compensation,
            contact=result.contact,
            headline=headline,
            decided_by=decided_by,
            ml_prob=ml_prob,
            rules_version=rules.rules_version_tag,
            model_version=model_version,
            status=status,
        )
        session.add(match)
        await session.commit()

        if will_notify:
            quiet = _in_quiet_hours(datetime.now(timezone.utc)) and final_score < 15
            if quiet:
                await redis.rpush(DIGEST_QUEUE, match.id)
            else:
                await redis.rpush(NOTIFY_QUEUE, match.id)


async def reload_watcher(state: dict) -> None:
    redis = get_redis()
    while True:
        try:
            val = await redis.get(KEY_RELOAD_SIGNAL)
            version = int(val) if val else 0
            if version != state.get("version", -1):
                state["rules"] = await load_rules_cache(version)
                state["version"] = version
                log.info("rules_reloaded", version=version)
        except Exception:
            log.exception("reload_watcher_failed")
        await asyncio.sleep(15)


async def main() -> None:
    redis = get_redis()
    try:
        await redis.xgroup_create(STREAM_RAW, STREAM_GROUP, id="0", mkstream=True)
    except Exception:
        pass  # группа уже существует

    state: dict = {"version": -1}
    state["rules"] = await load_rules_cache(0)
    state["version"] = 0
    source_cache = SourceCache()

    asyncio.create_task(reload_watcher(state))

    log.info("worker_started")
    while True:
        try:
            resp = await redis.xreadgroup(STREAM_GROUP, CONSUMER_NAME, {STREAM_RAW: ">"}, count=20, block=5000)
        except Exception:
            log.exception("xreadgroup_failed")
            await asyncio.sleep(2)
            continue
        if not resp:
            continue
        for _stream, messages in resp:
            for msg_id, fields in messages:
                try:
                    payload = json.loads(fields["data"])
                    await process_message(payload, state["rules"], source_cache, redis)
                except Exception:
                    log.exception("process_message_failed", msg_id=msg_id)
                finally:
                    await redis.xack(STREAM_RAW, STREAM_GROUP, msg_id)


if __name__ == "__main__":
    asyncio.run(main())
