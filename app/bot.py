import asyncio
import functools
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import structlog
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select, update

from app.config import settings
from app.db import SessionLocal
from app.models import Author, Feedback, Keyword, Match, RawMessage, RuleWeight, Source
from app.redis_client import (
    CHANNEL_ALERTS,
    DIGEST_QUEUE,
    KEY_BACKFILL_CMD,
    KEY_RELOAD_SIGNAL,
    NOTIFY_QUEUE,
    get_redis,
)
from app.filters import run_hard_filters
from app.normalize import apply_aliases, compute_struct_features, extract_headline, normalize_text
from app.rules_loader import load_rules_cache
from app.scoring import _matched_terms, score_message

log = structlog.get_logger("bot")

router = Router()

RATE_LIMIT_PER_MIN = 20
FEEDBACK_REASONS = [
    ("not_vacancy", "не вакансия"),
    ("not_my_stack", "не мой стек"),
    ("jobseeker_resume", "резюме соискателя"),
    ("closed", "вакансия закрыта"),
    ("discussion", "обсуждение, не вакансия"),
    ("course_ad", "курс/реклама"),
    ("duplicate", "дубль"),
]


def owner_only(handler):
    @functools.wraps(handler)  # сохраняем сигнатуру для DI aiogram (inspect.signature следует __wrapped__)
    async def wrapper(event, *args, **kwargs):
        user = event.from_user
        if user is None or user.id != settings.owner_telegram_id:
            return
        return await handler(event, *args, **kwargs)
    return wrapper


HANDLE_RE = re.compile(r"^(@\w{4,}|t\.me/\w+)$")


def _contact_url(contact: str | None) -> str | None:
    """Contact-регекс матчит и фразы вроде "пишите в лс" — валидная ссылка возможна только из @handle/t.me."""
    if not contact or not HANDLE_RE.match(contact):
        return None
    handle = contact.removeprefix("t.me/").lstrip("@")
    return f"https://t.me/{handle}"


def _card_keyboard(match_id: int, link: str | None, contact: str | None) -> InlineKeyboardMarkup:
    rows = []
    row1 = []
    if link:
        row1.append(InlineKeyboardButton(text="🔗 Открыть", url=link))
    contact_url = _contact_url(contact)
    if contact_url:
        row1.append(InlineKeyboardButton(text="✍️ Написать", url=contact_url))
    if row1:
        rows.append(row1)
    rows.append([
        InlineKeyboardButton(text="⭐ Избранное", callback_data=f"fav:{match_id}"),
        InlineKeyboardButton(text="🚫 Не то", callback_data=f"bad:{match_id}"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔍 Почему прислал", callback_data=f"why:{match_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_card(match: Match, raw: RawMessage, source_title: str, dup_note: str) -> str:
    stack_line = " · ".join(match.stack_matched) if match.stack_matched else "—"
    money_line = f"\n💰 {match.compensation}" if match.compensation else ""
    body = raw.text[:400]
    posted_local = raw.posted_at.astimezone(ZoneInfo(settings.tz)).strftime("%H:%M")
    headline = match.headline or "Вакансия"
    return (
        f"🎯 <b>{headline}</b> · {match.score} баллов\n\n"
        f"🧩 {stack_line}{money_line}\n\n"
        f"{body}\n\n"
        f"📡 {source_title}{dup_note}\n"
        f"🕐 {posted_local}"
    )


async def _load_card_data(match_id: int):
    async with SessionLocal() as session:
        match = (await session.execute(select(Match).where(Match.id == match_id))).scalar_one_or_none()
        if match is None:
            return None
        raw = (await session.execute(select(RawMessage).where(RawMessage.id == match.raw_message_id))).scalar_one()
        source = (await session.execute(select(Source).where(Source.id == raw.source_id))).scalar_one_or_none()
        source_title = source.title if source else str(raw.source_id)
        dup_note = f", ещё в {match.duplicate_count} источниках" if match.duplicate_count else ""
        return match, raw, source_title, dup_note, raw.link


async def notify_consumer(bot: Bot) -> None:
    redis = get_redis()
    sent_timestamps: list[float] = []
    while True:
        popped = await redis.blpop(NOTIFY_QUEUE, timeout=5)
        if popped is None:
            continue
        _key, match_id = popped
        match_id = int(match_id)
        if await redis.get("settings:paused"):
            await redis.rpush(NOTIFY_QUEUE, match_id)
            await asyncio.sleep(5)
            continue
        now = asyncio.get_event_loop().time()
        sent_timestamps[:] = [t for t in sent_timestamps if now - t < 60]
        if len(sent_timestamps) >= RATE_LIMIT_PER_MIN:
            await asyncio.sleep(60 - (now - sent_timestamps[0]))
        data = await _load_card_data(match_id)
        if data is None:
            continue
        match, raw, source_title, dup_note, link = data
        text = _format_card(match, raw, source_title, dup_note)
        kb = _card_keyboard(match.id, link, match.contact)
        try:
            msg = await bot.send_message(settings.owner_telegram_id, text, reply_markup=kb)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            msg = await bot.send_message(settings.owner_telegram_id, text, reply_markup=kb)
        except Exception:
            log.exception("notify_send_failed", match_id=match_id)
            continue
        sent_timestamps.append(now)
        async with SessionLocal() as session:
            await session.execute(
                update(Match)
                .where(Match.id == match_id)
                .values(status="notified", notified_at=datetime.now(timezone.utc), notification_msg_id=msg.message_id)
            )
            await session.commit()


async def dup_notify_consumer(bot: Bot) -> None:
    redis = get_redis()
    while True:
        popped = await redis.blpop("queue:dup_notify", timeout=5)
        if popped is None:
            continue
        _key, match_id = popped
        match_id = int(match_id)
        data = await _load_card_data(match_id)
        if data is None:
            continue
        match, raw, source_title, dup_note, link = data
        if not match.notification_msg_id:
            continue
        text = _format_card(match, raw, source_title, dup_note)
        try:
            await bot.edit_message_text(
                text,
                chat_id=settings.owner_telegram_id,
                message_id=match.notification_msg_id,
                reply_markup=_card_keyboard(match.id, link, match.contact),
            )
        except Exception:
            pass


async def alerts_subscriber(bot: Bot) -> None:
    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(CHANNEL_ALERTS)
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            await bot.send_message(settings.owner_telegram_id, f"⚠️ {message['data']}")
        except Exception:
            log.exception("alert_send_failed")


async def digest_job(bot: Bot) -> None:
    redis = get_redis()
    ids = []
    while True:
        val = await redis.lpop(DIGEST_QUEUE)
        if val is None:
            break
        ids.append(int(val))
    if not ids:
        return
    lines = [f"🌅 Утренний дайджест — {len(ids)} тихих совпадений за ночь:\n"]
    for mid in ids[:30]:
        data = await _load_card_data(mid)
        if data is None:
            continue
        match, raw, source_title, dup_note, link = data
        lines.append(f"• {match.score} баллов — {match.headline} ({source_title})" + (f"\n  {link}" if link else ""))
        async with SessionLocal() as session:
            await session.execute(
                update(Match).where(Match.id == mid).values(status="notified", notified_at=datetime.now(timezone.utc))
            )
            await session.commit()
    await bot.send_message(settings.owner_telegram_id, "\n".join(lines))


@router.message(Command("start"))
@owner_only
async def cmd_start(message: Message) -> None:
    await message.answer(
        "streamsift запущен.\n/help — список команд."
    )


@router.message(Command("help"))
@owner_only
async def cmd_help(message: Message) -> None:
    rules = await load_rules_cache(0)
    await message.answer(
        "/stats [день|неделя] — статистика\n"
        "/sources — список источников\n"
        "/add <@username|id> — добавить/включить источник + бэкафилл\n"
        "/mute <id> [часов] — заглушить источник\n"
        "/unmute <id> — снять заглушку\n"
        "/kind <id> <job_board|community|dm|other> — тип источника\n"
        "/keywords <dict_type> — показать словарь\n"
        "/keywords add <dict_type> <термин> [вес] — добавить термин\n"
        "/keywords del <dict_type> <термин> — удалить термин\n"
        "/weights — показать веса правил\n"
        "/weights set <key> <значение> — изменить вес\n"
        "/threshold N — порог уведомления (сейчас " + str(rules.notify_threshold) + ")\n"
        "/pause /resume — пауза/возобновление уведомлений\n"
        "/favorites — избранные\n"
        "/digest — прислать дайджест сейчас\n"
        "/dryrun N — прогнать последние N сообщений через текущие правила\n"
        "/retrain — статус/переобучение ML\n"
        "/why <match_id> — разбор баллов"
    )


@router.message(Command("stats"))
@owner_only
async def cmd_stats(message: Message, command: CommandObject) -> None:
    period = (command.args or "день").strip()
    days = 7 if period.startswith("нед") else 1
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with SessionLocal() as session:
        total_matches = (
            await session.execute(select(func.count()).select_from(Match).where(Match.created_at >= since))
        ).scalar_one()
        notified = (
            await session.execute(
                select(func.count()).select_from(Match).where(Match.created_at >= since, Match.status == "notified")
            )
        ).scalar_one()
        good = (
            await session.execute(
                select(func.count())
                .select_from(Feedback)
                .join(Match, Feedback.match_id == Match.id)
                .where(Feedback.created_at >= since, Feedback.verdict == "good")
            )
        ).scalar_one()
        bad = (
            await session.execute(
                select(func.count())
                .select_from(Feedback)
                .join(Match, Feedback.match_id == Match.id)
                .where(Feedback.created_at >= since, Feedback.verdict == "bad")
            )
        ).scalar_one()
    precision = good / (good + bad) if (good + bad) else None
    prec_str = f"{precision:.2f}" if precision is not None else "нет фидбека"
    await message.answer(
        f"За {period}:\nвсего совпадений: {total_matches}\nотправлено: {notified}\n"
        f"фидбек: 👍{good} / 👎{bad}\nprecision: {prec_str}"
    )


@router.message(Command("sources"))
@owner_only
async def cmd_sources(message: Message) -> None:
    async with SessionLocal() as session:
        rows = (await session.execute(select(Source).order_by(Source.title))).scalars().all()
    if not rows:
        await message.answer("Источников пока нет — ingest adapter ещё не синхронизировал список.")
        return
    lines = []
    for r in rows[:60]:
        flags = []
        if not r.enabled:
            flags.append("выкл")
        if r.muted_until and r.muted_until > datetime.now(timezone.utc):
            flags.append("muted")
        flag_s = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"{r.id} · {r.title} · {r.kind} · p{r.priority}{flag_s}")
    await message.answer("\n".join(lines))


@router.message(Command("add"))
@owner_only
async def cmd_add(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Использование: /add <id источника>. Источник должен быть доступен ingest adapter.")
        return
    arg = command.args.strip().lstrip("@")
    async with SessionLocal() as session:
        source = None
        if arg.lstrip("-").isdigit():
            source = (await session.execute(select(Source).where(Source.id == int(arg)))).scalar_one_or_none()
        else:
            source = (await session.execute(select(Source).where(Source.username == arg))).scalar_one_or_none()
        if source is None:
            await message.answer(
                "Не найден в известных источниках. Ingest adapter обновляет список источников "
                "каждые 30 минут — если источник появился недавно, дождись синхронизации и повтори."
            )
            return
        source.enabled = True
        await session.commit()
        redis = get_redis()
        await redis.rpush(KEY_BACKFILL_CMD, source.id)
    await message.answer(f"Источник «{source.title}» включён, запущен бэкафилл за {settings.source_backfill_days} дн.")


@router.message(Command("mute"))
@owner_only
async def cmd_mute(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if not parts:
        await message.answer("Использование: /mute <id> [часов]")
        return
    source_id = int(parts[0])
    hours = int(parts[1]) if len(parts) > 1 else 24 * 365
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    async with SessionLocal() as session:
        await session.execute(update(Source).where(Source.id == source_id).values(muted_until=until))
        await session.commit()
    await message.answer(f"Источник {source_id} заглушен на {hours}ч.")


@router.message(Command("unmute"))
@owner_only
async def cmd_unmute(message: Message, command: CommandObject) -> None:
    source_id = int((command.args or "0").strip())
    async with SessionLocal() as session:
        await session.execute(update(Source).where(Source.id == source_id).values(muted_until=None))
        await session.commit()
    await message.answer(f"Источник {source_id} размучен.")


@router.message(Command("kind"))
@owner_only
async def cmd_kind(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or parts[1] not in ("job_board", "community", "dm", "other"):
        await message.answer("Использование: /kind <id> <job_board|community|dm|other>")
        return
    source_id = int(parts[0])
    async with SessionLocal() as session:
        await session.execute(update(Source).where(Source.id == source_id).values(kind=parts[1]))
        await session.commit()
    await message.answer(f"Источник {source_id} -> kind={parts[1]}")


async def _bump_reload_signal() -> None:
    redis = get_redis()
    await redis.incr(KEY_RELOAD_SIGNAL)


@router.message(Command("keywords"))
@owner_only
async def cmd_keywords(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split(maxsplit=3)
    valid_types = {"stack_core", "stack_periph", "hiring_strong", "hiring_weak", "money", "anti", "closed", "noise", "alias"}
    if not args:
        await message.answer("Типы словарей: " + ", ".join(sorted(valid_types)))
        return
    if args[0] == "add" and len(args) >= 3:
        dict_type = args[1]
        term = args[2]
        weight = int(args[3]) if len(args) > 3 else 1
        if dict_type not in valid_types:
            await message.answer("Неизвестный dict_type.")
            return
        async with SessionLocal() as session:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = pg_insert(Keyword).values(dict_type=dict_type, term=term, weight=weight, enabled=True)
            stmt = stmt.on_conflict_do_update(
                index_elements=["dict_type", "term"], set_={"weight": weight, "enabled": True}
            )
            await session.execute(stmt)
            await session.commit()
        await _bump_reload_signal()
        await message.answer(f"Добавлено: {dict_type}/{term} (вес {weight})")
        return
    if args[0] == "del" and len(args) >= 3:
        dict_type, term = args[1], args[2]
        async with SessionLocal() as session:
            await session.execute(
                update(Keyword).where(Keyword.dict_type == dict_type, Keyword.term == term).values(enabled=False)
            )
            await session.commit()
        await _bump_reload_signal()
        await message.answer(f"Отключено: {dict_type}/{term}")
        return
    dict_type = args[0]
    if dict_type not in valid_types:
        await message.answer("Неизвестный dict_type. Типы: " + ", ".join(sorted(valid_types)))
        return
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Keyword).where(Keyword.dict_type == dict_type, Keyword.enabled.is_(True)).order_by(Keyword.term)
            )
        ).scalars().all()
    terms = ", ".join(f"{r.term}({r.weight})" for r in rows)
    await message.answer(f"{dict_type} [{len(rows)}]:\n{terms[:3800]}")


@router.message(Command("weights"))
@owner_only
async def cmd_weights(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if args and args[0] == "set" and len(args) == 3:
        key, value = args[1], int(args[2])
        async with SessionLocal() as session:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = pg_insert(RuleWeight).values(key=key, weight=value)
            stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"weight": value})
            await session.execute(stmt)
            await session.commit()
        await _bump_reload_signal()
        await message.answer(f"{key} = {value}")
        return
    async with SessionLocal() as session:
        rows = (await session.execute(select(RuleWeight).order_by(RuleWeight.key))).scalars().all()
    await message.answer("\n".join(f"{r.key} = {r.weight}" for r in rows) or "пусто")


@router.message(Command("threshold"))
@owner_only
async def cmd_threshold(message: Message, command: CommandObject) -> None:
    if not command.args:
        rules = await load_rules_cache(0)
        await message.answer(f"Текущий порог: {rules.notify_threshold}")
        return
    n = int(command.args.strip())
    async with SessionLocal() as session:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(RuleWeight).values(key="__notify_threshold__", weight=n)
        stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"weight": n})
        await session.execute(stmt)
        await session.commit()
    await _bump_reload_signal()
    await message.answer(f"Порог уведомления теперь {n}. Обязательно прогони /dryrun перед тем как полагаться на него.")


@router.message(Command("pause"))
@owner_only
async def cmd_pause(message: Message) -> None:
    redis = get_redis()
    await redis.set("settings:paused", "1")
    await message.answer("Уведомления на паузе. /resume — вернуть.")


@router.message(Command("resume"))
@owner_only
async def cmd_resume(message: Message) -> None:
    redis = get_redis()
    await redis.delete("settings:paused")
    await message.answer("Уведомления возобновлены.")


@router.message(Command("favorites"))
@owner_only
async def cmd_favorites(message: Message) -> None:
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(Match).where(Match.is_favorite.is_(True)).order_by(Match.created_at.desc()).limit(20))
        ).scalars().all()
    if not rows:
        await message.answer("Избранное пусто.")
        return
    lines = [f"{m.id} · {m.score}б · {m.headline}" for m in rows]
    await message.answer("\n".join(lines))


@router.message(Command("digest"))
@owner_only
async def cmd_digest(message: Message, bot: Bot) -> None:
    await digest_job(bot)
    await message.answer("Дайджест отправлен (если было что слать).")


@router.message(Command("retrain"))
@owner_only
async def cmd_retrain(message: Message) -> None:
    async with SessionLocal() as session:
        good = (await session.execute(select(func.count()).select_from(Feedback).where(Feedback.verdict == "good"))).scalar_one()
        total = (await session.execute(select(func.count()).select_from(Feedback))).scalar_one()
    if total < settings.ml_min_samples or good < 40:
        await message.answer(
            f"Разметки пока мало: {total}/{settings.ml_min_samples} (годных: {good}/40 нужно). "
            "ML остаётся выключен, работают только правила."
        )
        return
    await message.answer("Запускаю переобучение...")
    from app.train_ml import retrain as ml_retrain
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, ml_retrain)
    await message.answer(result)


@router.message(Command("dryrun"))
@owner_only
async def cmd_dryrun(message: Message, command: CommandObject) -> None:
    n = int((command.args or "50").strip())
    n = min(n, 500)
    rules = await load_rules_cache(0)
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(RawMessage).order_by(RawMessage.created_at.desc()).limit(n))
        ).scalars().all()
        sources = {s.id: s for s in (await session.execute(select(Source))).scalars().all()}

    would_notify = 0
    reject_counts: dict[str, int] = {}
    for raw in rows:
        text_norm = apply_aliases(normalize_text(raw.text), rules.alias_re, rules.aliases)
        struct = compute_struct_features(raw.text)
        stack_matched = _matched_terms(rules.stack_re, text_norm)
        reject = run_hard_filters(text_norm, stack_matched, raw.reply_to_id, rules.closed_re, rules.anti_re, rules.hiring_re)
        source = sources.get(raw.source_id)
        source_kind = source.kind if source else "other"
        if reject is None and not (rules.hiring_re and rules.hiring_re.search(text_norm)) and source_kind != "job_board":
            reject = "no_hiring_signal"
        if reject:
            reject_counts[reject] = reject_counts.get(reject, 0) + 1
            continue
        result = score_message(text_norm, struct, raw.reply_to_id, source_kind, source.priority if source else 0, 0, rules)
        if result.score >= rules.notify_threshold:
            would_notify += 1
    report_lines = [f"Dry-run на последних {len(rows)} сообщениях (текущие правила):", f"было бы отправлено: {would_notify}"]
    for rule, cnt in sorted(reject_counts.items(), key=lambda x: -x[1]):
        report_lines.append(f"  reject {rule}: {cnt}")
    await message.answer("\n".join(report_lines))


@router.message(Command("why"))
@owner_only
async def cmd_why(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Использование: /why <match_id>")
        return
    await _send_why(message.chat.id, int(command.args.strip()), message.bot)


async def _send_why(chat_id: int, match_id: int, bot: Bot) -> None:
    async with SessionLocal() as session:
        match = (await session.execute(select(Match).where(Match.id == match_id))).scalar_one_or_none()
    if match is None:
        await bot.send_message(chat_id, "Не найдено.")
        return
    lines = [f"Разбор баллов match #{match.id} (правила {match.rules_version}):"]
    for rule, points in match.signals.items():
        sign = "+" if points >= 0 else ""
        lines.append(f"  {rule}: {sign}{points}")
    lines.append(f"Итого: {match.score}")
    if match.decided_by == "ml":
        lines.append(f"Решение ML: p={match.ml_prob:.2f} (модель {match.model_version})")
    await bot.send_message(chat_id, "\n".join(lines))


@router.callback_query(F.data.startswith("why:"))
@owner_only
async def cb_why(call: CallbackQuery) -> None:
    match_id = int(call.data.split(":")[1])
    await _send_why(call.message.chat.id, match_id, call.bot)
    await call.answer()


@router.callback_query(F.data.startswith("fav:"))
@owner_only
async def cb_fav(call: CallbackQuery) -> None:
    """Избранное = одновременно сильный позитивный фидбек для reputation-модуля (FR-4.3)."""
    match_id = int(call.data.split(":")[1])
    async with SessionLocal() as session:
        match = (await session.execute(select(Match).where(Match.id == match_id))).scalar_one_or_none()
        if match is not None:
            match.is_favorite = True
            session.add(Feedback(match_id=match_id, verdict="good", reason=None))
            raw = (await session.execute(select(RawMessage).where(RawMessage.id == match.raw_message_id))).scalar_one_or_none()
            if raw is not None and raw.sender_id is not None:
                author = (await session.execute(select(Author).where(Author.sender_id == raw.sender_id))).scalar_one_or_none()
                if author is None:
                    author = Author(sender_id=raw.sender_id, good_count=1)
                    session.add(author)
                else:
                    author.good_count += 1
        await session.commit()
    await call.answer("Добавлено в избранное")


@router.callback_query(F.data.startswith("bad:"))
@owner_only
async def cb_bad(call: CallbackQuery) -> None:
    match_id = int(call.data.split(":")[1])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=f"reason:{match_id}:{code}")] for code, label in FEEDBACK_REASONS]
    )
    await call.message.answer("Почему не то?", reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("reason:"))
@owner_only
async def cb_reason(call: CallbackQuery) -> None:
    _, match_id_s, reason = call.data.split(":")
    match_id = int(match_id_s)
    async with SessionLocal() as session:
        session.add(Feedback(match_id=match_id, verdict="bad", reason=reason))
        match = (await session.execute(select(Match).where(Match.id == match_id))).scalar_one_or_none()
        if match is not None:
            raw = (await session.execute(select(RawMessage).where(RawMessage.id == match.raw_message_id))).scalar_one_or_none()
            if raw is not None and raw.sender_id is not None:
                author = (await session.execute(select(Author).where(Author.sender_id == raw.sender_id))).scalar_one_or_none()
                if author is None:
                    author = Author(sender_id=raw.sender_id, bad_count=1)
                    session.add(author)
                else:
                    author.bad_count += 1
        await session.commit()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Учтено")


async def main() -> None:
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=settings.tz)
    end_h, end_m = settings.quiet_hours_end.split(":")
    scheduler.add_job(digest_job, "cron", hour=int(end_h), minute=int(end_m), args=[bot])
    scheduler.start()

    log.info("bot_started")
    await asyncio.gather(
        dp.start_polling(bot),
        notify_consumer(bot),
        dup_notify_consumer(bot),
        alerts_subscriber(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
