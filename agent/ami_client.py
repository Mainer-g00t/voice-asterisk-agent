"""
Minimal AMI client for the agent container — only used for call transfer (Redirect).

The full AMI client (originate, etc.) lives in config-api/ami_client.py.
"""

import asyncio
import os
import uuid
from loguru import logger

AMI_HOST = os.environ.get("AMI_HOST", "asterisk")
AMI_PORT = int(os.environ.get("AMI_PORT", "5038"))
AMI_USER = os.environ.get("AMI_USER", "voiceagent")
AMI_SECRET = os.environ.get("AMI_SECRET", "")

_CONNECT_TIMEOUT = 5.0
_RESPONSE_TIMEOUT = 10.0


async def _read_message(reader: asyncio.StreamReader) -> dict[str, str]:
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


async def _read_response(reader, action_id: str) -> dict[str, str]:
    for _ in range(50):
        msg = await asyncio.wait_for(_read_message(reader), timeout=_RESPONSE_TIMEOUT)
        if not msg:
            continue
        if "Response" in msg and msg.get("ActionID", "") == action_id:
            return msg
        if "Response" in msg and "ActionID" not in msg:
            return msg
    raise RuntimeError("AMI response not received after 50 messages")


async def _write_message(writer: asyncio.StreamWriter, fields: dict[str, str]) -> None:
    msg = "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
    writer.write(msg.encode())
    await writer.drain()


async def ami_redirect(channel: str, destination: str, context: str = "default") -> None:
    """
    Issue an AMI Redirect action to transfer a channel to a new extension.

    `channel` — the Asterisk channel name (e.g. PJSIP/softphone-00000001)
    `destination` — dialplan extension or number to redirect to
    `context` — dialplan context (default: "default")
    """
    if not AMI_SECRET:
        logger.warning("[AMI] AMI_SECRET not set — skipping redirect")
        return

    action_id = str(uuid.uuid4())

    logger.info(f"[AMI] Redirect channel={channel!r} → {destination!r} (ctx={context})")

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(AMI_HOST, AMI_PORT),
        timeout=_CONNECT_TIMEOUT,
    )

    try:
        # Banner
        await reader.readline()

        # Login
        await _write_message(writer, {
            "Action": "Login",
            "Username": AMI_USER,
            "Secret": AMI_SECRET,
            "ActionID": action_id + "-login",
        })
        login_resp = await _read_response(reader, action_id + "-login")
        if login_resp.get("Response") != "Success":
            raise RuntimeError(f"AMI login failed: {login_resp.get('Message')}")

        # Redirect
        await _write_message(writer, {
            "Action": "Redirect",
            "Channel": channel,
            "Exten": destination,
            "Context": context,
            "Priority": "1",
            "ActionID": action_id,
        })
        resp = await _read_response(reader, action_id)
        logger.info(f"[AMI] Redirect response: {resp.get('Response')} — {resp.get('Message', '')}")

        await _write_message(writer, {"Action": "Logoff"})

    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
