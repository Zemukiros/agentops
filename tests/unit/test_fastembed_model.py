"""The real embedding model (ADR-010). Marker `model`: downloads bge-small (~67 MB) once into
FASTEMBED_CACHE_PATH; not part of `make test`. Run: uv run pytest -m model"""

import math

import pytest

from agentops.memory.embedder import FastEmbedder
from agentops.settings import EMBEDDING_DIM, Settings

pytestmark = pytest.mark.model


async def test_bge_small_dimensions_and_semantics() -> None:
    e = FastEmbedder(Settings(embedding_mode="fastembed"))
    a, b, z = await e.embed(
        [
            "How do I store embeddings in Postgres?",
            "pgvector adds a vector column type to PostgreSQL.",
            "My sourdough starter smells like acetone.",
        ]
    )
    assert len(a) == EMBEDDING_DIM == 384
    assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-6)
    cos = lambda u, v: sum(x * y for x, y in zip(u, v, strict=True))  # noqa: E731
    print(f"\nrelated={cos(a, b):.3f} unrelated={cos(a, z):.3f}")
    assert cos(a, b) > cos(a, z) + 0.1
