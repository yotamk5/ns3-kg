"""Python extraction via the stdlib ast module."""

from __future__ import annotations

import ast


def extract(src: bytes) -> dict:
    tree = ast.parse(src.decode("utf8", "replace"))

    symbols: list[dict] = []
    base_classes: list[dict] = []
    includes: list[dict] = []

    def add_symbol(node, kind, name, qname, sig, parent_tmp):
        symbols.append(
            {
                "tmp": id(node),
                "parent_tmp": parent_tmp,
                "kind": kind,
                "name": name,
                "qualified_name": qname,
                "signature": sig,
                "start_line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
                "order": (node.lineno, node.col_offset),
            }
        )

    def visit(node, scope: list[str], class_tmp: int | None):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                qname = ".".join(scope + [child.name])
                bases_txt = ", ".join(ast.unparse(b) for b in child.bases)
                sig = f"class {child.name}({bases_txt})" if bases_txt else f"class {child.name}"
                add_symbol(child, "class", child.name, qname, sig, class_tmp)
                for b in child.bases:
                    base_classes.append(
                        {
                            "class_tmp": id(child),
                            "base_name": ast.unparse(b),
                            "access": None,
                        }
                    )
                visit(child, scope + [child.name], id(child))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = ".".join(scope + [child.name])
                kind = "method_def" if isinstance(node, ast.ClassDef) else "function"
                sig = f"def {child.name}({ast.unparse(child.args)})"
                if child.returns is not None:
                    sig += f" -> {ast.unparse(child.returns)}"
                add_symbol(child, kind, child.name, qname, sig,
                           class_tmp if isinstance(node, ast.ClassDef) else None)
                visit(child, scope + [child.name], None)
            elif isinstance(child, ast.Import):
                for a in child.names:
                    includes.append({"path": a.name, "is_system": 0})
            elif isinstance(child, ast.ImportFrom):
                includes.append({"path": child.module or ".", "is_system": 0})
            else:
                visit(child, scope, class_tmp)

    visit(tree, [], None)

    return {
        "symbols": symbols,
        "base_classes": base_classes,
        "includes": includes,
        "calls": [],
    }
