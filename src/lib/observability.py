import logging
import structlog

def setup_logging():
    structlog.configure(
        processors=[
            structlog.processors.JSONRenderer()
        ]
    )
