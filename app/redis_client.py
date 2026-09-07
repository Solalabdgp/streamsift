import redis.asyncio as redis

from app.config import settings

STREAM_RAW = "queue:raw"
STREAM_GROUP = "workers"
CONSUMER_WORKER = "worker-1"

NOTIFY_QUEUE = "queue:notify"  # list of match_id, pushed by worker, popped by bot
DIGEST_QUEUE = "queue:digest"  # list of match_id collected during quiet hours

KEY_DEDUP_EXACT = "dedup:exact:{hash}"
KEY_SIMHASH_BAND = "dedup:simhash:band:{i}:{v}"  # SET of raw_message_id

KEY_HEARTBEAT_INGEST = "heartbeat:ingest"
KEY_RATE_NOTIFY = "rate:notify"  # token bucket, list of timestamps

KEY_RELOAD_SIGNAL = "signal:reload_rules"  # incremented on /keywords /weights /threshold
KEY_BACKFILL_CMD = "cmd:backfill"  # list of source_id, popped by the ingest adapter

CHANNEL_ALERTS = "channel:alerts"  # pub/sub -> bot forwards to owner


def get_redis() -> redis.Redis:
    return redis.from_url(settings.redis_url, decode_responses=True)
