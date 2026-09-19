import math

from fastembed import TextEmbedding


EMBEDDING_DIM = 384

_model: TextEmbedding | None = None


def _get_model() -> TextEmbedding:
    global _model

    if _model is None:
        _model = TextEmbedding(
            model_name="sentence-transformers/all-MiniLM-L6-v2"
        )

    return _model


def generate_embedding(text: str) -> list[float]:
    """
    Generate a normalized 384-dimensional embedding.

    Uses FastEmbed/ONNX instead of PyTorch.
    """
    vector = list(_get_model().embed([text]))[0]

    norm = math.sqrt(sum(float(x) * float(x) for x in vector))

    if norm == 0:
        return [0.0] * EMBEDDING_DIM

    return [float(x) / norm for x in vector]
