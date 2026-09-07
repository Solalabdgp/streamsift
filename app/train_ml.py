"""
Переобучение ML-классификатора (FR-6.3/6.4/6.5). Источник разметки — таблица
feedback (👍/👎 с карточек). Держим последние 5 версий модели, новую версию
принимаем только если её метрики на отложенной выборке не хуже текущей.

Синхронный модуль (sklearn), вызывается через run_in_executor из bot.py
(/retrain) или из celery beat (еженедельно).
"""
import glob
import os
import time

import joblib
from sklearn.metrics import precision_score, recall_score
from sklearn.model_selection import train_test_split
from sqlalchemy import create_engine, text

from app.config import settings
from app.ml import build_pipeline

SYNC_DB_URL = settings.database_url.replace("postgresql+asyncpg", "postgresql+psycopg2")


def _load_dataset() -> tuple[list[str], list[int]]:
    engine = create_engine(SYNC_DB_URL)
    query = text(
        """
        SELECT rm.text_norm, f.verdict
        FROM feedback f
        JOIN matches m ON m.id = f.match_id
        JOIN raw_messages rm ON rm.id = m.raw_message_id
        """
    )
    with engine.connect() as conn:
        rows = conn.execute(query).fetchall()
    texts = [r[0] for r in rows]
    labels = [1 if r[1] == "good" else 0 for r in rows]
    return texts, labels


def _current_model_metrics(x_holdout, y_holdout) -> tuple[float, float] | None:
    paths = sorted(glob.glob(os.path.join(settings.ml_model_dir, "clf_*.joblib")))
    if not paths:
        return None
    model = joblib.load(paths[-1])
    preds = model.predict(x_holdout)
    return precision_score(y_holdout, preds, zero_division=0), recall_score(y_holdout, preds, zero_division=0)


def retrain() -> str:
    texts, labels = _load_dataset()
    if len(texts) < settings.ml_min_samples or sum(labels) < 40:
        return f"Недостаточно разметки: {len(texts)}/{settings.ml_min_samples} (good: {sum(labels)}/40)."

    x_train, x_hold, y_train, y_hold = train_test_split(
        texts, labels, test_size=0.2, stratify=labels, random_state=42
    )

    pipeline = build_pipeline()
    pipeline.fit(x_train, y_train)
    preds = pipeline.predict(x_hold)
    new_precision = precision_score(y_hold, preds, zero_division=0)
    new_recall = recall_score(y_hold, preds, zero_division=0)

    current = _current_model_metrics(x_hold, y_hold)
    if current is not None:
        cur_precision, cur_recall = current
        if new_precision < cur_precision or new_recall < cur_recall:
            return (
                f"Новая модель хуже текущей (precision {new_precision:.2f} vs {cur_precision:.2f}, "
                f"recall {new_recall:.2f} vs {cur_recall:.2f}) — откат, модель НЕ заменена."
            )

    os.makedirs(settings.ml_model_dir, exist_ok=True)
    version = time.strftime("%Y%m%d%H%M%S")
    path = os.path.join(settings.ml_model_dir, f"clf_{version}.joblib")
    joblib.dump(pipeline, path)

    # держим последние 5 версий
    all_paths = sorted(glob.glob(os.path.join(settings.ml_model_dir, "clf_*.joblib")))
    for old_path in all_paths[:-5]:
        os.remove(old_path)

    return f"Новая модель {version}: precision={new_precision:.2f}, recall={new_recall:.2f}. Сохранена."
