"""
Minimal asyncio Asterisk Manager Interface (AMI) client.

Used only for outbound call origination — connects, logs in, sends one
Originate action, reads the response, disconnects. No persistent connection.

AMI protocol: plain text over TCP. Messages are key:value lines separated by
CRLF, with a blank line (CRLFCRLF) terminating each message.
"""

import asyncio
import os
import uuid
from loguru import logger


AMI_HOST = os.environ.get("AMI_HOST", "asterisk")
AMI_PORT = int(os.environ.get("AMI_PORT", "5038"))
AMI_USER = os.environ.get("AMI_USER", "voiceagent")
AMI_SECRET = os.environ.get("AMI_SECRET", "voiceai_ami_secret")

_CONNECT_TIMEOUT = 5.0
_RESPONSE_TIMEOUT = 10.0


async def _read_message(reader: asyncio.StreamReader) -> dict[str, str]:
    """Read one AMI message (blank-line terminated) and parse into a dict."""
    lines = []
    while True:
        line = await reader.readline()
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if text == "":
            break
        if ":" in text:
            key, _, val = text.partition(":")
            lines.append((key.strip(), val.strip()))
    return dict(lines)


async def _read_response(reader: asyncio.StreamReader, action_id: str) -> dict[str, str]:
    """
    Read AMI messages until we find a Response (not an Event) matching action_id.
    AMI may deliver events between our request and the response — skip them.
    """
    for _ in range(50):  # safety cap
        msg = await asyncio.wait_for(_read_message(reader), timeout=_RESPONSE_TIMEOUT)
        if not msg:
            continue
        # Skip events (they have "Event" key, not "Response")
        if "Response" in msg and msg.get("ActionID", "") == action_id:
            return msg
        # Also accept if no ActionID in response (some AMI versions omit it)
        if "Response" in msg and "ActionID" not in msg:
            return msg
    raise RuntimeError("AMI response not received after 50 messages")


async def _write_message(writer: asyncio.StreamWriter, fields: dict[str, str]) -> None:
    """Serialize and send one AMI message."""
    msg = "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
    writer.write(msg.encode())
    await writer.drain()


async def originate(
    *,
    channel: str,
    call_uuid: str,
    agent_slug: str,
    destination: str,
    caller_id: str = "Voice Agent <+10000000000>",
    timeout_ms: int = 30_000,
) -> dict:
    """
    Originate an outbound call via AMI.

    Returns the AMI response dict on success.
    Raises RuntimeError on login failure or Originate failure.
    """
    action_id = str(uuid.uuid4())

    logger.info(
        f"[AMI] Originating call → channel={channel!r} uuid={call_uuid} "
        f"agent={agent_slug!r} destination={destination!r}"
    )

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(AMI_HOST, AMI_PORT),
        timeout=_CONNECT_TIMEOUT,
    )

    try:
        # Read AMI banner (e.g. "Asterisk Call Manager/6.0.0")
        banner = await reader.readline()
        logger.debug(f"[AMI] banner: {banner.decode().strip()}")

        # Login
        await _write_message(writer, {
            "Action": "Login",
            "Username": AMI_USER,
            "Secret": AMI_SECRET,
            "ActionID": action_id + "-login",
        })
        login_resp = await _read_response(reader, action_id + "-login")
        logger.debug(f"[AMI] login response: {login_resp}")
        if login_resp.get("Response") != "Success":
            raise RuntimeError(f"AMI login failed: {login_resp.get('Message', login_resp)}")

        # Originate
        await _write_message(writer, {
            "Action": "Originate",
            "Channel": channel,
            "Context": "outbound-agent",
            "Exten": "s",
            "Priority": "1",
            "CallerID": caller_id,
            "Timeout": str(timeout_ms),
            "Variable": f"CALL_UUID={call_uuid},AGENT_SLUG={agent_slug},DESTINATION={destination}",
            "ActionID": action_id,
            "Async": "true",   # return immediately, don't block until call completes
        })
        orig_resp = await _read_response(reader, action_id)
        logger.debug(f"[AMI] originate response: {orig_resp}")

        if orig_resp.get("Response") not in ("Success", "Queued"):
            raise RuntimeError(
                f"AMI Originate failed: {orig_resp.get('Message', orig_resp)}"
            )

        # Logoff
        await _write_message(writer, {"Action": "Logoff"})

    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    logger.info(f"[AMI] Originate accepted for call {call_uuid}")
    return orig_resp
