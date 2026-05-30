"""
Custom Pipecat frame types for the voice-asterisk-agent.

SystemFrames bypass the normal pipeline queue and are processed immediately,
so they don't get stuck behind queued audio frames.
"""

from dataclasses import dataclass, field
from pipecat.frames.frames import SystemFrame


@dataclass
class DTMFInputFrame(SystemFrame):
    """
    Emitted by AudioSocketInputTransport when Asterisk sends a DTMF digit.
    Flows upstream through the pipeline so FlowWatcherProcessor can catch it.
    """
    digit: str = ""


@dataclass
class FlowTransitionFrame(SystemFrame):
    """
    Emitted by FlowWatcherProcessor when a flow edge condition fires.
    Caught by server.py to drive node actions (transfer, end, say, etc.).
    """
    node_id: str = ""
    node_type: str = ""
    node_config: dict = field(default_factory=dict)
    edge_id: str = ""
