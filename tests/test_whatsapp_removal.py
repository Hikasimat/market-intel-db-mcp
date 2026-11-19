"""Tests verifying complete removal of WhatsApp MCP tools, files, routes, endpoints, and settings."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import api_client
import config
from conftest import valid_prospect
from server import create_mcp

SERVER_ROOT = Path(__file__).resolve().parent.parent


def test_deleted_whatsapp_modules_cannot_be_imported():
    for mod_name in ("whatsapp_workflow", "whatsapp_schemas", "whatsapp_qr_views"):
        assert not (SERVER_ROOT / f"{mod_name}.py").exists(), f"{mod_name}.py should be deleted"
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(mod_name)
    assert not (SERVER_ROOT / "tests" / "test_whatsapp.py").exists()


def test_no_whatsapp_references_in_runtime_code_or_config():
    forbidden_terms = ("whatsapp", "waha", "wa_session", "qrbase64", "qr_url", "whatsapp_allowed_roles")
    runtime_files = [
        p for p in SERVER_ROOT.rglob("*.py")
        if "tests" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts
    ]
    runtime_files += [
        SERVER_ROOT / ".env.example",
        SERVER_ROOT / "Dockerfile",
        SERVER_ROOT / "docker-compose.yml",
        SERVER_ROOT / ".github" / "workflows" / "deploy.yml",
    ]
    for path in runtime_files:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for term in forbidden_terms:
            assert term not in text, f"Found residual WhatsApp reference {term!r} in {path}"


def test_config_has_no_whatsapp_settings():
    assert not hasattr(config, "VALID_WHATSAPP_ROLES")
    s = config.Settings(api_base_url="https://api.test/api", username="u", password="p")
    for field in vars(s):
        assert "whatsapp" not in field.lower()


async def test_mcp_has_no_whatsapp_tools_or_routes(wf):
    mcp = create_mcp(lambda: wf)
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}
    assert not any("whatsapp" in name.lower() or "qr" in name.lower() for name in tool_names)

    app = mcp.streamable_http_app()
    paths = [getattr(r, "path", "") for r in app.routes]
    assert not any("/whatsapp" in p for p in paths)
    assert not any("/whatsapp" in path for _, path in api_client.ALLOWED_ENDPOINTS)
    assert not api_client.is_endpoint_allowed("GET", "/whatsapp/status/")
    assert not api_client.is_endpoint_allowed("POST", "/whatsapp/send/")

    # Non-WhatsApp prospect workflow remains intact
    val = await wf.validate(valid_prospect())
    assert val["valid"] and val["confirmation_token"]
