from scripts.admin.actions.builtins import register_builtins
from scripts.admin.actions.registry import ActionContext, ActionRegistry, ActionSpec

register_builtins(ActionRegistry)

__all__ = [
    "ActionContext",
    "ActionRegistry",
    "ActionSpec",
    "register_builtins",
]
