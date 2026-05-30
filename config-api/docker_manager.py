"""
Docker SDK wrapper — manages agent containers and reloads Asterisk dialplan.

Mounts required in docker-compose.yml:
  config-api:
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - .:/workspace                              # project root, writable
"""

import os
import re

import docker
from docker.errors import NotFound, ImageNotFound
from loguru import logger

AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "voice-asterisk-agent-agent")
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "voice-asterisk-agent_voiceai")
WORKSPACE = os.environ.get("WORKSPACE", "/workspace")
ASTERISK_CONTAINER = os.environ.get("ASTERISK_CONTAINER", "asterisk")

_client: docker.DockerClient | None = None


def get_client() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def _agent_env(slug: str) -> dict:
    """Build environment for an agent-{slug} container."""
    env = {
        "AGENT_SLUG": slug,
        "AUDIOSOCKET_HOST": "0.0.0.0",
        "AUDIOSOCKET_PORT": "9099",
        "METRICS_PORT": "9090",
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        "CONFIG_API_URL": os.environ.get("CONFIG_API_URL", "http://config-api:8080"),
        "LOGURU_LEVEL": os.environ.get("LOGURU_LEVEL", "INFO"),
    }
    # Pass through any cloud API keys that are set
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def container_name(slug: str) -> str:
    return f"agent-{slug}"


def ensure_agent_running(slug: str) -> str:
    """
    Start agent-{slug} container if not already running.
    Returns "started" | "already_running" | "restarted".
    """
    client = get_client()
    name = container_name(slug)
    env = _agent_env(slug)

    try:
        container = client.containers.get(name)
        if container.status == "running":
            # Check if AGENT_SLUG env is correct (slug may have changed)
            current_env = {
                e.split("=", 1)[0]: e.split("=", 1)[1]
                for e in container.attrs["Config"]["Env"]
                if "=" in e
            }
            if current_env.get("AGENT_SLUG") == slug:
                logger.info(f"Container '{name}' already running")
                return "already_running"
        # Wrong config or not running — remove and recreate
        logger.info(f"Removing stale container '{name}'")
        container.remove(force=True)
    except NotFound:
        pass

    logger.info(f"Starting container '{name}' (slug={slug!r})")
    try:
        client.containers.run(
            AGENT_IMAGE,
            name=name,
            environment=env,
            network=DOCKER_NETWORK,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
        )
        return "started"
    except ImageNotFound:
        raise RuntimeError(
            f"Agent image '{AGENT_IMAGE}' not found. Run 'make up' first to build it."
        )


def stop_agent(slug: str) -> str:
    """Stop and remove agent-{slug} container. Returns 'stopped' | 'not_found'."""
    client = get_client()
    name = container_name(slug)
    try:
        container = client.containers.get(name)
        container.remove(force=True)
        logger.info(f"Stopped and removed container '{name}'")
        return "stopped"
    except NotFound:
        return "not_found"


def list_running_agents() -> list[dict]:
    """Return status of all agent-* containers."""
    client = get_client()
    containers = client.containers.list(all=True, filters={"name": "agent-"})
    return [
        {
            "name": c.name,
            "slug": c.name.removeprefix("agent-"),
            "status": c.status,
        }
        for c in containers
        if c.name.startswith("agent-")
    ]


def reload_asterisk_dialplan() -> str:
    """Run 'dialplan reload' inside the Asterisk container."""
    client = get_client()
    try:
        container = client.containers.get(ASTERISK_CONTAINER)
        exit_code, output = container.exec_run(
            "asterisk -rx 'dialplan reload'", user="root"
        )
        output_str = output.decode() if output else ""
        if exit_code == 0:
            logger.info(f"Asterisk dialplan reloaded: {output_str.strip()}")
            return output_str.strip()
        else:
            raise RuntimeError(f"Asterisk dialplan reload failed (exit {exit_code}): {output_str}")
    except NotFound:
        raise RuntimeError(f"Asterisk container '{ASTERISK_CONTAINER}' not found")


_SAFE_DID  = re.compile(r'^[0-9+*#_X.]+$')
_SAFE_SLUG = re.compile(r'^[a-z0-9-]+$')


def _sanitize_dialplan_str(value: str) -> str:
    """Strip newlines and other control characters from a string destined for extensions.conf."""
    return re.sub(r'[\r\n\x00-\x1f\x7f]', '', value)


def _validate_route_fields(did: str, slug: str) -> None:
    """Raise ValueError if did or slug contain characters that could inject dialplan lines."""
    if not _SAFE_DID.match(did) and did != "_X.":
        raise ValueError(f"Invalid DID '{did}': only digits, +, *, #, _, X, . are allowed")
    if not _SAFE_SLUG.match(slug):
        raise ValueError(f"Invalid agent_slug '{slug}': only lowercase letters, digits, and hyphens are allowed")


def write_extensions_conf(routes: list[dict], default_slug: str = "basic") -> str:
    """
    Generate and write extensions.conf from active phone routes.
    Returns the generated content.
    """
    lines = [
        "; Auto-generated by config-api — do not edit manually.",
        "; Edit routes in the admin UI and click Apply.",
        "",
        "[from-softphone]",
    ]

    # CONFIG_API_URL inside the voiceai Docker network
    config_api = "http://config-api:8080"

    for route in routes:
        did = route["did"]
        slug = route["agent_slug"]
        desc = _sanitize_dialplan_str(route.get("description") or f"Route {did} → {slug}")
        cname = container_name(slug)

        try:
            _validate_route_fields(did, slug)
        except ValueError as exc:
            logger.warning(f"Skipping route with invalid fields: {exc}")
            continue

        if did == "_X.":
            # Catch-all — add last
            continue

        lines += [
            f"; {desc}",
            f"exten => {did},1,NoOp(Routing {did} to {slug})",
            f" same => n,Answer()",
            f" same => n,Set(CALL_UUID=${{SHELL(head -c 36 /proc/sys/kernel/random/uuid)}})",
            f" same => n,Set(CURL_RESULT=${{CURL({config_api}/internal/calls/pre-register,caller_id=${{CALLERID(num)}}&did={did}&call_uuid=${{CALL_UUID}})}})",
            f" same => n,AudioSocket(${{CALL_UUID}},{cname}:9099)",
            f" same => n,Hangup()",
            "",
        ]

    # Catch-all last (either from routes or the default)
    catchall = next((r for r in routes if r["did"] == "_X."), None)
    catchall_slug = catchall["agent_slug"] if catchall else default_slug
    if not _SAFE_SLUG.match(catchall_slug):
        logger.warning(f"Invalid catch-all slug '{catchall_slug}', falling back to 'basic'")
        catchall_slug = "basic"
    catchall_cname = container_name(catchall_slug)
    lines += [
        "; Default catch-all",
        "exten => _X.,1,NoOp(Default route → " + catchall_slug + ")",
        " same => n,Answer()",
        " same => n,Set(CALL_UUID=${SHELL(head -c 36 /proc/sys/kernel/random/uuid)})",
        f" same => n,Set(CURL_RESULT=${{CURL({config_api}/internal/calls/pre-register,caller_id=${{CALLERID(num)}}&did=_X.&call_uuid=${{CALL_UUID}})}})",
        f" same => n,AudioSocket(${{CALL_UUID}},{catchall_cname}:9099)",
        " same => n,Hangup()",
    ]

    # Outbound context — always included so AMI Originate can use it.
    lines += [
        "",
        "; ── Outbound calls (originated via API) ─────────────────────────────────────",
        "[outbound-agent]",
        "exten => s,1,NoOp(Outbound to ${DESTINATION} via agent-${AGENT_SLUG})",
        " same => n,Answer()",
        " same => n,Wait(1)",
        " same => n,AudioSocket(${CALL_UUID},agent-${AGENT_SLUG}:9099)",
        " same => n,Hangup()",
    ]

    content = "\n".join(lines) + "\n"
    path = os.path.join(WORKSPACE, "asterisk", "extensions.conf")
    with open(path, "w") as f:
        f.write(content)
    logger.info(f"Wrote extensions.conf ({len(routes)} routes) to {path}")
    return content
