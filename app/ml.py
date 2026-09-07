"""
ML-классификатор серой зоны (FR-6.x). Выключен по умолчанию (ML_ENABLED=false) —
активируется только после накопления ML_MIN_SAMPLES (150, из них >=40 good)
размеченных примеров в таблице feedback + автоматических reject'ов с ручной
верификацией. Пока модели нет — classify() возвращает None, вызывающий код
(worker.py) в этом случае откатывается на чистое пороговое правило.
"""
import glob
import os

import joblib
import structlog
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from app.config import settings

log = structlog.get_logger("ml")

_model_cache: tuple[str, Pipeline] | None = None


def _latest_model_path() -> str | None:
    paths = sorted(glob.glob(os.path.join(settings.ml_model_dir, "clf_*.joblib")))
    return paths[-1] if paths else None


def classify(text_norm: str) -> tuple[float, str] | None:
    global _model_cache
    if not settings.ml_enabled:
        return None
    path = _latest_model_path()
    if path is None:
        log.warning("ml_enabled_but_no_model")
        return None
    if _model_cache is None or _model_cache[0] != path:
        try:
            _model_cache = (path, joblib.load(path))
        except Exception:
            log.exception("ml_model_load_failed", path=path)
            return None
    _, model = _model_cache
    prob = float(model.predict_proba([text_norm])[0][1])
    version = os.path.basename(path).removeprefix("clf_").removesuffix(".joblib")
    return prob, version


def build_pipeline() -> Pipeline:
    return Pipeline([
        ("tfidf", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2)),
        ("clf", LogisticRegression(class_weight="balanced", max_iter=1000)),
    ])
