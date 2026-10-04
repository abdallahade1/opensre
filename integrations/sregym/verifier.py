"""SREGym MCP integration verifier."""

from __future__ import annotations

from integrations.sregym import build_sregym_config, validate_sregym_config
from integrations.verification import register_validation_verifier

verify_sregym = register_validation_verifier(
    "sregym",
    build_config=build_sregym_config,
    validate_config=validate_sregym_config,
)
