"""
Tool handler registry — maps handler_type strings (stored in DB/Redis) to
Python async callables. New handler types require a code deploy; everything
else (prompts, specialist configs, tool schemas) is data-driven.
"""

from .specialist_router import make_specialist_handler

HANDLER_REGISTRY: dict[str, callable] = {
    "specialist_router": make_specialist_handler,
    # Add future handler types here:
    # "transfer_call": make_transfer_handler,
}
