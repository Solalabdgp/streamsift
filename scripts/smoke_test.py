"""Разовый скрипт: кладёт тестовые сообщения в очередь, чтобы проверить пайплайн end-to-end. Не часть прод-кода."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from app.redis_client import STREAM_RAW, get_redis

NOW = datetime.now(timezone.utc)

CASES = [
    {
        "name": "good_vacancy",
        "text": (
            "Ищем Python разработчика в команду.\n\n"
            "Требования: опыт Python от 2 лет, FastAPI, PostgreSQL, Docker, Redis.\n"
            "Обязанности: разработка backend, интеграция Telegram-бота на aiogram.\n"
            "Мы предлагаем: оплата от 250к, удалёнка, гибкий график.\n\n"
            "Пишите в лс @hr_manager_test"
        ),
    },
    {
        "name": "jobseeker_resume",
        "text": (
            "Ищу работу Python разработчиком, опыт 3 года.\n"
            "Мой стек: FastAPI, Docker, PostgreSQL, aiogram.\n"
            "Вот моё резюме, пишите в лс @candidate_test"
        ),
    },
    {
        "name": "closed_vacancy",
        "text": "Вакансия Python backend разработчик уже закрыта, всем спасибо за отклики и интерес!",
    },
    {
        "name": "off_stack",
        "text": (
            "Ищем 1C программиста в команду.\n"
            "Требования: опыт 1С от 3 лет.\n"
            "Обязанности: доработка конфигураций.\n"
            "Оплата 150к. Пишите в лс @hr_1c_test"
        ),
    },
]


async def main() -> None:
    redis = get_redis()
    for i, case in enumerate(CASES):
        payload = {
            "source_id": 90001,
            "source_title": "Smoke Test Source",
            "source_username": None,
            "source_is_channel": True,
            "source_is_group": False,
            "source_kind_guess": "community",
            "message_id": 1000 + i,
            "thread_id": None,
            "sender_id": 999999,
            "reply_to_id": None,
            "posted_at": (NOW - timedelta(seconds=i)).isoformat(),
            "text": case["text"],
            "permalink": None,
            "is_forward": False,
            "grouped_id": None,
            "is_backfill": False,
        }
        await redis.xadd(STREAM_RAW, {"data": json.dumps(payload, ensure_ascii=False)})
        print(f"queued: {case['name']}")


if __name__ == "__main__":
    asyncio.run(main())
