import time
from dataclasses import dataclass

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Source

REFRESH_INTERVAL_SEC = 60


@dataclass
class SourceInfo:
    id: int
    title: str
    kind: str
    enabled: bool
    muted_until: object
    priority: int


class SourceCache:
    def __init__(self) -> None:
        self._by_id: dict[int, SourceInfo] = {}
        self._loaded_at: float = 0.0

    async def _refresh(self) -> None:
        async with SessionLocal() as session:
            rows = (await session.execute(select(Source))).scalars().all()
        self._by_id = {
            r.id: SourceInfo(r.id, r.title, r.kind, r.enabled, r.muted_until, r.priority) for r in rows
        }
        self._loaded_at = time.time()

    async def get(self, source_id: int) -> SourceInfo | None:
        if time.time() - self._loaded_at > REFRESH_INTERVAL_SEC:
            await self._refresh()
        return self._by_id.get(source_id)
