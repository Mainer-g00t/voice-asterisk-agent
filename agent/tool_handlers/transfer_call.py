"""
Transfer call handler — initiates a call transfer to a destination number.

Currently a functional stub: logs the transfer request and returns a confirmation
to the LLM so the conversation can close gracefully. Full Asterisk AMI integration
(which requires the AMI socket and authentication) is deferred.

Expected tool parameters:
  - destination (string): phone number or extension to transfer to
  - reason      (string, optional): reason for transfer
"""

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams


def make_transfer_call_handler(agent_config: dict, tool_config: dict | None = None):
    """
    Returns an async handler that logs a transfer request and tells the LLM
    the transfer is being initiated.
    """

    async def _handler(params: FunctionCallParams) -> None:
        destination = params.arguments.get("destination", "")
        reason = params.arguments.get("reason", "")

        if not destination:
            await params.result_callback({"error": "No destination provided"})
            return

        logger.info(
            f"[transfer_call] Transfer requested → destination={destination!r} reason={reason!r}"
        )

        # TODO: implement Asterisk AMI transfer here
        # AMI action: Redirect / Bridge / Transfer depending on the channel type
        # Requires: manager.conf credentials + asyncio AMI client

        await params.result_callback({
            "status": "transfer_initiated",
            "destination": destination,
            "message": f"Transferring your call to {destination}. Please hold.",
        })

    return _handler
