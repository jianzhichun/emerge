"""Backward-compatibility shim — real code lives in scripts/mcp/schemas.py."""
from scripts.mcp.schemas import get_tool_schemas

__all__ = ["get_tool_schemas"]
