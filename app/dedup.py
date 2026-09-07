import hashlib
import re

from redis.asyncio import Redis

from app.redis_client import KEY_DEDUP_EXACT, KEY_SIMHASH_BAND

SIMHASH_BITS = 64
SIMHASH_BANDS = 4
BAND_BITS = SIMHASH_BITS // SIMHASH_BANDS  # 16
HAMMING_THRESHOLD = 3
DEDUP_TTL_SECONDS = 30 * 24 * 3600  # exact: 30 дней
SIMHASH_TTL_SECONDS = 14 * 24 * 3600  # simhash: 14 дней

_TOKEN_RE = re.compile(r"[a-zа-я0-9_]{3,}", re.IGNORECASE)


def text_hash(text_norm: str) -> str:
    return hashlib.sha256(text_norm.encode("utf-8")).hexdigest()


def simhash64(text_norm: str) -> int:
    """Простой word-level simhash: каждый токен -> 64-битный хеш, голосование по битам."""
    tokens = _TOKEN_RE.findall(text_norm)
    if not tokens:
        return 0
    bits = [0] * SIMHASH_BITS
    for token in tokens:
        h = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")
        for i in range(SIMHASH_BITS):
            bits[i] += 1 if (h >> i) & 1 else -1
    result = 0
    for i in range(SIMHASH_BITS):
        if bits[i] > 0:
            result |= 1 << i
    return result


MASK64 = 0xFFFFFFFFFFFFFFFF


def to_signed64(v: int) -> int:
    """Postgres BIGINT — signed 64-bit; simhash считаем как unsigned, конвертируем на границе с БД."""
    v &= MASK64
    return v - 0x10000000000000000 if v >= 0x8000000000000000 else v


def to_unsigned64(v: int) -> int:
    return v & MASK64


def hamming_distance(a: int, b: int) -> int:
    return bin(to_unsigned64(a) ^ to_unsigned64(b)).count("1")


def _band_value(simhash: int, band_index: int) -> int:
    return (simhash >> (band_index * BAND_BITS)) & ((1 << BAND_BITS) - 1)


async def exact_duplicate_of(redis: Redis, h: str) -> int | None:
    val = await redis.get(KEY_DEDUP_EXACT.format(hash=h))
    return int(val) if val is not None else None


async def register_exact(redis: Redis, h: str, raw_message_id: int) -> None:
    # NX: не перетираем существующий "оригинал" повторной регистрацией дубля
    await redis.set(KEY_DEDUP_EXACT.format(hash=h), str(raw_message_id), ex=DEDUP_TTL_SECONDS, nx=True)


async def simhash_candidates(redis: Redis, simhash: int) -> set[int]:
    """ID сообщений, у которых совпал хотя бы один band — кандидаты на проверку Hamming."""
    candidates: set[int] = set()
    for i in range(SIMHASH_BANDS):
        key = KEY_SIMHASH_BAND.format(i=i, v=_band_value(simhash, i))
        members = await redis.smembers(key)
        candidates.update(int(m) for m in members)
    return candidates


async def register_simhash(redis: Redis, simhash: int, raw_message_id: int) -> None:
    for i in range(SIMHASH_BANDS):
        key = KEY_SIMHASH_BAND.format(i=i, v=_band_value(simhash, i))
        await redis.sadd(key, raw_message_id)
        ttl = await redis.ttl(key)
        if ttl < 0:
            await redis.expire(key, SIMHASH_TTL_SECONDS)
