# streamsift

A Python service that consumes a stream of text messages, decides which ones are job postings for a given tech stack, and delivers the survivors to a Telegram bot. Scoring is deterministic: keyword dictionaries and rule weights stored in Postgres, no LLM in the hot path.

This repository contains the processing, storage, and delivery halves of the system. The source adapter that fills the ingest queue is kept in a separate private repository, because it carries account credentials and is specific to one deployment. Everything here runs against a documented JSON payload, so any adapter that produces that payload works. `scripts/smoke_test.py` writes fixture payloads into the queue, which is enough to exercise the whole pipeline end to end without an adapter at all.

## Pipeline

```
  ingest adapter            worker                     bot
  (out of scope,     ┌──────────────────────┐    ┌──────────────┐
   see contract)     │ normalize            │    │  aiogram     │
        │            │ dedup: sha256        │    │  inline card │
        │  Redis     │        + simhash64   │    │  + feedback  │
        └─ Stream ──▶│ hard filters HF-1..7 │    │    buttons   │
          queue:raw  │ scoring              │    └──────▲───────┘
                     │ ML (grey zone, off)  │           │
                     └──────────┬───────────┘    queue:notify
                                │                       │
                                ▼                       │
                        ┌───────────────┐               │
                        │ PostgreSQL 16 │───────────────┘
                        │  sources      │
                        │  raw_messages │       ┌──────────────┐
                        │  matches      │◀──────│ celery beat  │
                        │  feedback     │       │ FP report    │
                        │  keywords     │       │ retention    │
                        │  rule_weights │       │ heartbeat    │
                        │  authors      │       │ retrain      │
                        └───────────────┘       └──────────────┘
```

Redis carries three things at once: the ingest stream (a consumer group, so the worker can be restarted without losing messages), the dedup index, and the notify/digest lists the bot pops from. Postgres holds everything that has to survive a flush.

## Input contract

The worker reads `queue:raw` and expects one JSON object per entry:

| field | type | meaning |
| --- | --- | --- |
| `source_id` | int | stable id of the origin |
| `source_title` | str | display name, shown on the card |
| `source_kind` | str | `job_board` / `community` / `direct` / `other`, feeds scoring |
| `message_id` | int | id within the source |
| `thread_id` | int \| null | thread the message belongs to |
| `sender_id` | int \| null | author id, used for author reputation |
| `reply_to_id` | int \| null | set when the message is a reply |
| `posted_at` | str | ISO-8601, UTC |
| `text` | str | message body |
| `permalink` | str \| null | link back to the message, if the source has one |
| `is_forward` | bool | forwarded content |
| `is_backfill` | bool | historical replay rather than live traffic |

Anything missing is treated as null. An adapter that can produce this shape needs no other coupling to the pipeline.

## Processing stages

**Normalization** (`app/normalize.py`) strips markdown characters, lowercases, folds `ё` to `е`, collapses runs of spaces and blank lines. Structural features come off the *raw* text before any of that: length, line count, bullet lines, links, mentions, hashtag count. A separate alias pass rewrites transliterations and synonyms onto canonical terms, so `питон`, `пайтон` and `python3` all score as `python`.

**Deduplication** (`app/dedup.py`) runs in two passes. Exact matches are a sha256 of the normalized text, held in Redis for 30 days. Near-duplicates use a 64-bit word-level simhash, indexed in four 16-bit bands with a 14-day TTL; a band collision produces candidates, and Hamming distance ≤ 3 confirms the duplicate. The simhash is computed unsigned and converted at the Postgres boundary, since `BIGINT` is signed.

**Hard filters** (`app/filters.py`) reject before any scoring happens, first hit wins:

| rule | rejects |
| --- | --- |
| HF-1 | normalized length outside 40..8000 characters |
| HF-2 | no stack term anywhere in the text |
| HF-3 | a "position closed" marker, either in the first 200 characters or within 5 tokens of a hiring term |
| HF-4 | short text (< 200 chars) in question form |
| HF-5 | job-seeker phrasing, same proximity rule as HF-3 |
| HF-6 | a reply shorter than 200 characters |
| HF-7 | duplicate, resolved earlier in the dedup stage |

The proximity check exists because "закрыта" and "ищу" show up inside perfectly valid postings. Distance to the nearest hiring term is what separates "vacancy closed" from "we closed the last round, now hiring again".

**Scoring** (`app/scoring.py`) adds five contributions:

- stack terms, capped by `STACK_SCORE_CAP`
- hiring and compensation terms, capped by `HIRING_SCORE_CAP`
- structural features: section headers, contact handle, a money pattern, bullet lists, length bonus and penalty, reply penalty, hashtag spam penalty
- source context: source kind, manual source priority, author reputation earned from past feedback
- a noise penalty for course and advertising vocabulary, floored by `NOISE_PENALTY_CAP`

The total goes against `NOTIFY_THRESHOLD`, with a grey band between `GREY_LOW` and `GREY_HIGH`. Every contribution is recorded in the `signals` JSONB column, so `/why <match_id>` in the bot can print the arithmetic that produced any given score.

Dictionaries and rule weights live in the `keywords` and `rule_weights` tables, seeded by migration `0002`. Editing them from the bot bumps a Redis counter; the worker watches it and reloads its compiled regex cache without a restart.

**Optional classifier** (`app/ml.py`, `app/train_ml.py`) is a TF-IDF char n-gram (3..5) into logistic regression with balanced class weights. It is off by default and only ever runs on grey-zone messages. It stays off until the `feedback` table holds at least 150 labeled examples with at least 40 positives, which is checked in code rather than left as a config comment. When no model file is present, `classify()` returns `None` and the worker falls back to the plain threshold.

## Delivery

The bot is aiogram 3.15 on the public Bot API. Every handler is wrapped in an owner check against `OWNER_TELEGRAM_ID`, so the bot ignores everyone else. Each match arrives as a card with the headline, the matched stack terms, compensation if one was detected, the first 400 characters of the body, and the source name. Inline buttons cover open, contact, favorite, reject, and the score breakdown. Pressing reject asks for a reason from a fixed list, and that answer becomes a labeled row in `feedback` plus a reputation adjustment for the author.

During quiet hours matches go to a digest list instead of firing immediately, and get sent in one batch at the end of the window. Notifications are rate limited to 20 per minute, with `TelegramRetryAfter` handled rather than swallowed.

Operational commands: `/sources`, `/add`, `/mute`, `/unmute`, `/kind`, `/keywords`, `/weights`, `/threshold`, `/stats`, `/favorites`, `/digest`, `/dryrun`, `/retrain`, `/why`. `/dryrun N` replays the last N stored messages through the current rules without sending anything, which is how rule edits get checked before they go live.

## Scheduled work

Celery beat runs four jobs in its own process, away from the message pipeline:

- weekly false-positive report over the `feedback` table
- retention cleanup of old rejected `raw_messages`
- an ingest heartbeat check every 10 minutes, alerting through the bot when the key goes stale
- weekly retraining, which only replaces the active model if it beats the current one on holdout

## Stack

Python 3.12, Redis 7, PostgreSQL 16, Docker Compose. SQLAlchemy 2.0 async with asyncpg, psycopg2-binary for Alembic, Alembic 1.14 for migrations. Celery 5.4 for scheduled work, APScheduler inside the bot process for the digest window. pydantic-settings for config, structlog for logging, scikit-learn and joblib for the optional classifier.

## Running it

```bash
cp .env.example .env          # fill BOT_TOKEN and OWNER_TELEGRAM_ID
docker compose build
docker compose up -d postgres redis
docker compose run --rm migrate
docker compose up -d worker bot beat
python scripts/smoke_test.py  # push fixture payloads through the pipeline
docker compose logs -f worker bot
```

The smoke fixtures cover four cases: a matching posting, a job-seeker post that HF-5 should reject, a closed vacancy that HF-3 should reject, and an off-stack posting that should score below the threshold.

## Tests

`app/scoring.py` and `app/filters.py` are pure functions with no I/O, and the tests in `tests/` cover them directly. `pytest` runs the suite; GitHub Actions runs the same suite on every push.

## Known gaps

- Grouped media messages are not merged into a single logical message. It has not broken anything so far, because in practice one item in a group carries the caption, but that is luck rather than design.
- Training data comes only from manual feedback. Automatic rejects never get reviewed, so the classifier never learns from them.
- The scoring dictionaries are tuned for Russian-language postings with English technical terms mixed in. Other languages need their own seed data.

## Note on history

This service was developed privately and is published here as a subset of a larger codebase. The git history starts at the import commit rather than at the original first commit.

## License

MIT.
