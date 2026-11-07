"""Shared helpers: embedding model and SQLite connections."""
import os
import sqlite3

import sqlite_vec

MODEL = "intfloat/multilingual-e5-small"
DIM = 384


def load_model():
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    # fastembed does not list this model, so register it as a custom model.
    TextEmbedding.add_custom_model(
        model=MODEL,
        pooling=PoolingType.MEAN,
        normalization=True,
        sources=ModelSource(hf=MODEL),
        dim=DIM,
        model_file="onnx/model.onnx",
    )
    return TextEmbedding(MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE"))


def connect(path, readonly=False):
    uri = f"file:{path}?mode={'ro' if readonly else 'rwc'}"
    db = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db
