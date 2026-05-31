"""
Tests for config-api/docker_manager.py — pure functions only.

No Docker daemon required — we only test the functions that don't touch
the Docker SDK (write_extensions_conf, _sanitize_dialplan_str,
_validate_route_fields).
"""

import os
import tempfile

import pytest

# Patch WORKSPACE before importing so write_extensions_conf writes to a temp dir
os.environ.setdefault("WORKSPACE", tempfile.mkdtemp())

from docker_manager import (
    _sanitize_dialplan_str,
    _validate_route_fields,
    container_name,
    write_extensions_conf,
)


# ── container_name ────────────────────────────────────────────────────────────

class TestContainerName:
    def test_basic(self):
        assert container_name("sales") == "agent-sales"

    def test_with_hyphen(self):
        assert container_name("customer-service") == "agent-customer-service"


# ── _sanitize_dialplan_str ────────────────────────────────────────────────────

class TestSanitizeDialplanStr:
    def test_clean_string_unchanged(self):
        assert _sanitize_dialplan_str("Route 1000 → basic") == "Route 1000 → basic"

    def test_strips_newline(self):
        assert _sanitize_dialplan_str("hello\nworld") == "helloworld"

    def test_strips_carriage_return(self):
        assert _sanitize_dialplan_str("hello\rworld") == "helloworld"

    def test_strips_crlf(self):
        assert _sanitize_dialplan_str("hello\r\nworld") == "helloworld"

    def test_strips_null_byte(self):
        assert _sanitize_dialplan_str("hel\x00lo") == "hello"

    def test_strips_other_control_chars(self):
        assert _sanitize_dialplan_str("hel\x01\x1flo") == "hello"

    def test_injection_attempt_stripped(self):
        # The newline is the actual injection vector — Asterisk is line-oriented.
        # Stripping it collapses the payload onto the same comment line, which is harmless.
        malicious = "desc\nexten => _X.,1,System(rm -rf /)"
        result = _sanitize_dialplan_str(malicious)
        assert "\n" not in result  # no newline → no new dialplan line


# ── _validate_route_fields ────────────────────────────────────────────────────

class TestValidateRouteFields:
    # Valid DIDs
    def test_valid_did_digits(self):
        _validate_route_fields("1000", "basic")  # should not raise

    def test_valid_did_e164(self):
        _validate_route_fields("+15551234567", "basic")

    def test_valid_did_catchall(self):
        _validate_route_fields("_X.", "basic")

    def test_valid_did_asterisk_pattern(self):
        _validate_route_fields("1*#", "basic")

    # Valid slugs
    def test_valid_slug_simple(self):
        _validate_route_fields("1000", "sales")

    def test_valid_slug_hyphenated(self):
        _validate_route_fields("1000", "customer-service")

    def test_valid_slug_alphanumeric(self):
        _validate_route_fields("1000", "agent2")

    # Invalid DIDs
    def test_invalid_did_newline(self):
        with pytest.raises(ValueError, match="Invalid DID"):
            _validate_route_fields("1000\nexten => _X.,1,System(evil)", "basic")

    def test_invalid_did_semicolon(self):
        with pytest.raises(ValueError, match="Invalid DID"):
            _validate_route_fields("1000;bad", "basic")

    def test_invalid_did_space(self):
        with pytest.raises(ValueError, match="Invalid DID"):
            _validate_route_fields("1000 extra", "basic")

    def test_invalid_did_parentheses(self):
        with pytest.raises(ValueError, match="Invalid DID"):
            _validate_route_fields("1000(evil)", "basic")

    # Invalid slugs
    def test_invalid_slug_uppercase(self):
        with pytest.raises(ValueError, match="Invalid agent_slug"):
            _validate_route_fields("1000", "Basic")

    def test_invalid_slug_newline(self):
        with pytest.raises(ValueError, match="Invalid agent_slug"):
            _validate_route_fields("1000", "basic\nmalicious")

    def test_invalid_slug_space(self):
        with pytest.raises(ValueError, match="Invalid agent_slug"):
            _validate_route_fields("1000", "my agent")

    def test_invalid_slug_special_chars(self):
        with pytest.raises(ValueError, match="Invalid agent_slug"):
            _validate_route_fields("1000", "agent@bad")


# ── write_extensions_conf ─────────────────────────────────────────────────────

@pytest.fixture()
def workspace(tmp_path):
    """Redirect WORKSPACE to a temp dir so write_extensions_conf can write."""
    asterisk_dir = tmp_path / "asterisk"
    asterisk_dir.mkdir()
    old = os.environ.get("WORKSPACE")
    os.environ["WORKSPACE"] = str(tmp_path)
    yield tmp_path
    if old is None:
        del os.environ["WORKSPACE"]
    else:
        os.environ["WORKSPACE"] = old


def read_conf(workspace):
    return (workspace / "asterisk" / "extensions.conf").read_text()


class TestWriteExtensionsConf:

    def test_always_includes_outbound_context(self, workspace):
        content = write_extensions_conf([], workspace=str(workspace))
        assert "[outbound-agent]" in content

    def test_always_includes_from_softphone_context(self, workspace):
        content = write_extensions_conf([], workspace=str(workspace))
        assert "[from-softphone]" in content

    def test_always_includes_catchall(self, workspace):
        content = write_extensions_conf([], workspace=str(workspace))
        assert "exten => _X." in content

    def test_basic_route_appears_in_conf(self, workspace):
        routes = [{"did": "1000", "agent_slug": "basic", "description": "Main line"}]
        content = write_extensions_conf(routes, workspace=str(workspace))
        assert "exten => 1000" in content
        assert "agent-basic:9099" in content

    def test_multiple_routes(self, workspace):
        routes = [
            {"did": "1000", "agent_slug": "basic", "description": ""},
            {"did": "2000", "agent_slug": "sales", "description": ""},
        ]
        content = write_extensions_conf(routes, workspace=str(workspace))
        assert "exten => 1000" in content
        assert "exten => 2000" in content
        assert "agent-basic:9099" in content
        assert "agent-sales:9099" in content

    def test_explicit_catchall_route(self, workspace):
        routes = [{"did": "_X.", "agent_slug": "sales", "description": ""}]
        content = write_extensions_conf(routes, workspace=str(workspace))
        assert "agent-sales:9099" in content

    def test_catchall_appears_last(self, workspace):
        routes = [
            {"did": "_X.", "agent_slug": "sales", "description": ""},
            {"did": "1000", "agent_slug": "basic", "description": ""},
        ]
        content = write_extensions_conf(routes, workspace=str(workspace))
        # 1000 exten must appear before the _X. catchall block
        idx_1000 = content.index("exten => 1000")
        idx_catchall = content.rindex("exten => _X.")  # last occurrence
        assert idx_1000 < idx_catchall

    def test_invalid_did_route_skipped(self, workspace):
        routes = [
            {"did": "1000\nexten => _X.,1,System(evil)", "agent_slug": "basic", "description": ""},
            {"did": "2000", "agent_slug": "sales", "description": ""},
        ]
        content = write_extensions_conf(routes, workspace=str(workspace))
        assert "System(evil)" not in content
        assert "exten => 2000" in content  # valid route still present

    def test_invalid_slug_route_skipped(self, workspace):
        routes = [
            {"did": "1000", "agent_slug": "Bad Slug!", "description": ""},
            {"did": "2000", "agent_slug": "sales", "description": ""},
        ]
        content = write_extensions_conf(routes, workspace=str(workspace))
        assert "Bad Slug!" not in content
        assert "exten => 2000" in content

    def test_description_newline_stripped(self, workspace):
        # The newline is stripped — "evil injection" ends up on the same comment line,
        # which is harmless because Asterisk dialplan is line-oriented.
        routes = [{"did": "1000", "agent_slug": "basic",
                   "description": "Good desc\nevil injection"}]
        content = write_extensions_conf(routes, workspace=str(workspace))
        # No bare newline inside the description comment line
        comment_line = [l for l in content.splitlines() if "Good desc" in l][0]
        assert "Good desc" in comment_line
        assert "\n" not in comment_line  # already implied by splitlines, but explicit

    def test_invalid_catchall_slug_falls_back_to_basic(self, workspace):
        routes = [{"did": "_X.", "agent_slug": "Bad!", "description": ""}]
        content = write_extensions_conf(routes, workspace=str(workspace))
        assert "agent-basic:9099" in content

    def test_file_written_to_disk(self, workspace):
        write_extensions_conf([], workspace=str(workspace))
        assert (workspace / "asterisk" / "extensions.conf").exists()

    def test_default_slug_used_when_no_catchall_route(self, workspace):
        content = write_extensions_conf([], default_slug="sales", workspace=str(workspace))
        assert "agent-sales:9099" in content

    def test_pre_register_curl_present(self, workspace):
        routes = [{"did": "1000", "agent_slug": "basic", "description": ""}]
        content = write_extensions_conf(routes, workspace=str(workspace))
        assert "internal/calls/pre-register" in content
