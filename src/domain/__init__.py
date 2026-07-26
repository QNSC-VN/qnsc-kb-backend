"""Domain package.

Submodules are intentionally not imported eagerly. This keeps lightweight domain
tests independent from optional worker/LLM dependencies and avoids import cycles.
Import services from their concrete module, for example ``src.domain.permissions``.
"""
