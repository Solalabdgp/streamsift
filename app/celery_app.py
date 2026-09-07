import asyncio
from datetime import datetime, timedelta, timezone

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Feedback, Match, RawMessage, Source
from app.redis_client import CHANNEL_ALERTS, KEY_HEARTBEAT_INGEST, get_redis

celery_app = Celery("jobradar", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.timezone = settings.tz
celery_app.conf.beat_schedule = {
    "weekly-fp-report": {"task": "app.celery_app.weekly_fp_report", "schedule": crontab(day_of_week=1, hour=9, minute=0)},
    "weekly-retrain": {"task": "app.celery_app.weekly_retrain", "schedule": crontab(day_of_week=1, hour=9, minute=15)},
    "retention-cleanup": {"task": "app.celery_app.retention_cleanup", "schedule": crontab(hour=4, minute=30)},
    "heartbeat-check": {"task": "app.celery_app.heartbeat_check", "schedule": 600.0},
}


def _run(coro):
    return asyncio.run(coro)


async def _alert(text: str) -> None:
    redis = get_redis()
    await redis.publish(CHANNEL_ALERTS, text)


async def _weekly_fp_report_async() -> None:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    async with SessionLocal() as session:
        notified = (
            await session.execute(select(func.count()).select_from(Match).where(Match.notified_at >= since))
        ).scalar_one()
        good = (
            await session.execute(
                select(func.count()).select_from(Feedback).where(Feedback.created_at >= since, Feedback.verdict == "good")
            )
        ).scalar_one()
        bad = (
            await session.execute(
                select(func.count()).select_from(Feedback).where(Feedback.created_at >= since, Feedback.verdict == "bad")
            )
        ).scalar_one()

        bad_by_source = (
            await session.execute(
                select(Source.title, func.count())
                .select_from(Feedback)
                .join(Match, Feedback.match_id == Match.id)
                .join(RawMessage, RawMessage.id == Match.raw_message_id)
                .join(Source, Source.id == RawMessage.source_id)
                .where(Feedback.created_at >= since, Feedback.verdict == "bad")
                .group_by(Source.id, Source.title)
                .order_by(func.count().desc())
                .limit(5)
            )
        ).all()

        tp_count = (
            await session.execute(
                select(func.count())
                .select_from(Feedback)
                .join(Match, Feedback.match_id == Match.id)
                .where(Feedback.created_at >= since, Feedback.verdict == "good")
            )
        ).scalar_one()

    precision = good / (good + bad) if (good + bad) else None
    lines = [
        "📊 Еженедельный отчёт по качеству:",
        f"отправлено уведомлений: {notified}",
        f"фидбек: 👍{good} / 👎{bad}",
        f"precision по фидбеку: {precision:.2f}" if precision is not None else "фидбека пока нет",
        "топ-5 источников по FP:",
    ]
    for title, cnt in bad_by_source:
        lines.append(f"  {title}: {cnt}")

    # FR-9.2: источники с >10 FP и <1 TP за 14 дней
    since14 = datetime.now(timezone.utc) - timedelta(days=14)
    async with SessionLocal() as session:
        fp_rows = (
            await session.execute(
                select(Source.id, Source.title, func.count())
                .select_from(Feedback)
                .join(Match, Feedback.match_id == Match.id)
                .join(RawMessage, RawMessage.id == Match.raw_message_id)
                .join(Source, Source.id == RawMessage.source_id)
                .where(Feedback.created_at >= since14, Feedback.verdict == "bad")
                .group_by(Source.id, Source.title)
                .having(func.count() > 10)
            )
        ).all()
    for source_id, title, fp_cnt in fp_rows:
        lines.append(f"⚠️ источник «{title}» ({source_id}): {fp_cnt} FP за 14 дней — рассмотри /mute {source_id}")

    _run(_alert("\n".join(lines)))


@celery_app.task
def weekly_fp_report() -> None:
    asyncio.run(_weekly_fp_report_async())


@celery_app.task
def weekly_retrain() -> None:
    from app.train_ml import retrain

    result = retrain()
    asyncio.run(_alert(f"🔄 Еженедельное переобучение ML: {result}"))


async def _retention_cleanup_async() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    deleted_total = 0
    async with SessionLocal() as session:
        while True:
            result = await session.execute(
                select(RawMessage.id)
                .where(RawMessage.reject_rule.is_not(None), RawMessage.created_at < cutoff)
                .limit(10_000)
            )
            ids = [r[0] for r in result.all()]
            if not ids:
                break
            from sqlalchemy import delete

            await session.execute(delete(RawMessage).where(RawMessage.id.in_(ids)))
            await session.commit()
            deleted_total += len(ids)
    return deleted_total


@celery_app.task
def retention_cleanup() -> None:
    deleted = asyncio.run(_retention_cleanup_async())
    if deleted:
        asyncio.run(_alert(f"🧹 Retention cleanup: удалено {deleted} старых отклонённых raw_messages."))


@celery_app.task
def heartbeat_check() -> None:
    async def _check() -> None:
        redis = get_redis()
        val = await redis.get(KEY_HEARTBEAT_INGEST)
        if val is None:
            await _alert("🔴 Ingest adapter down: heartbeat отсутствует.")
            return
        age = int(datetime.now(timezone.utc).timestamp()) - int(val)
        if age > 900:
            await _alert(f"🔴 Ingest adapter down {age // 60} мин — проверь контейнер.")

    asyncio.run(_check())
