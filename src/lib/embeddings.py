import structlog
import threading
from sentence_transformers import SentenceTransformer
from src.core.config import settings

logger = structlog.get_logger()

class BGEModelSingleton:
    _model = None
    _lock = threading.Lock()

    @classmethod
    def get_model(cls) -> SentenceTransformer:
        if cls._model is None:
            with cls._lock:
                if cls._model is None:
                    logger.info("Initializing SentenceTransformer model (loading weights)...", model=settings.EMBEDDING_MODEL)
                    try:
                        # Lazy loading SentenceTransformer model
                        cls._model = SentenceTransformer(settings.EMBEDDING_MODEL)
                        logger.info("SentenceTransformer model loaded successfully.")
                    except Exception as e:
                        logger.error("Failed to load SentenceTransformer model", error=str(e), model=settings.EMBEDDING_MODEL)
                        raise e
        return cls._model

def get_bge_embedding(text: str) -> list[float]:
    """
    Computes a 1024-dimensional embedding vector locally for the given text.
    Uses normalization to align with cosine similarity metrics.
    """
    # For testing or runtimes where model loading is bypassed, check settings
    if settings.OPENAI_API_KEY == "mock" or settings.EMBEDDING_MODEL == "mock":
        return [0.0] * settings.EMBEDDING_DIMENSION
        
    try:
        model = BGEModelSingleton.get_model()
        # Normalization ensures embeddings can be compared using dot product or cosine distance directly
        vector = model.encode(text, normalize_embeddings=True)
        return vector.tolist()
    except Exception as e:
        logger.error("Error generating local embedding", error=str(e))
        # Fallback dummy vector of correct dimension
        return [0.0] * settings.EMBEDDING_DIMENSION
