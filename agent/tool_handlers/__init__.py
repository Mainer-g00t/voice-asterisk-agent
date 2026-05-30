"""
Tool handler registry — maps handler_type strings (stored in DB/Redis) to
Python async callables. New handler types require a code deploy; everything
else (prompts, specialist configs, tool schemas) is data-driven.
"""

from .specialist_router import make_specialist_handler
from .webhook import make_webhook_handler
from .transfer_call import make_transfer_call_handler

HANDLER_REGISTRY: dict[str, callable] = {
    "specialist_router": make_specialist_handler,
    "webhook":           make_webhook_handler,
    "transfer_call":     make_transfer_call_handler,
}
