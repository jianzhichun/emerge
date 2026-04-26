"""Static checks for pipeline code artifacts."""
from __future__ import annotations

import ast


def code_assigns_name(code: str, name: str) -> bool:
    """Return True when ``name`` appears as an assignment target in ``code``.

    Covers plain assignment, annotated assignment, augmented assignment, tuple
    unpacking, and subscript writes such as ``globals()["__result"] = ...``.
    Syntax errors return False so callers can refuse invalid artifact code.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.found = False

        def _check_target(self, node: ast.AST) -> None:
            if isinstance(node, ast.Name) and node.id == name:
                self.found = True
            elif isinstance(node, (ast.Tuple, ast.List)):
                for element in node.elts:
                    self._check_target(element)

        def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
            for target in node.targets:
                self._check_target(target)
            self.generic_visit(node)

        def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
            self._check_target(node.target)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
            self._check_target(node.target)
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
            if isinstance(node.slice, ast.Constant) and node.slice.value == name:
                self.found = True
            self.generic_visit(node)

    visitor = _Visitor()
    visitor.visit(tree)
    return visitor.found
