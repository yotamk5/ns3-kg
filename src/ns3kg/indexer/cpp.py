"""C++ extraction via tree-sitter, driven by the .scm query files in queries/."""

from __future__ import annotations

import re
from pathlib import Path

import tree_sitter_cpp
from tree_sitter import Language, Parser, Query, QueryCursor

_LANG = Language(tree_sitter_cpp.language())
_PARSER = Parser(_LANG)
_QUERY_DIR = Path(__file__).parent / "queries"
_QUERIES: dict[str, Query] = {}

_MAX_SIG = 500
_CLASS_TYPES = ("class_specifier", "struct_specifier")


def _query(name: str) -> Query:
    q = _QUERIES.get(name)
    if q is None:
        q = Query(_LANG, (_QUERY_DIR / f"{name}.scm").read_text(encoding="utf8"))
        _QUERIES[name] = q
    return q


def _matches(name: str, root):
    return QueryCursor(_query(name)).matches(root)


def _text(node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf8", "replace")


def _oneline(s: str) -> str:
    return " ".join(s.split())[:_MAX_SIG]


def _enclosing_class(node):
    """Nearest ancestor class/struct definition node, or None."""
    cur = node.parent
    while cur is not None:
        if cur.type in _CLASS_TYPES and cur.child_by_field_name("body") is not None:
            return cur
        cur = cur.parent
    return None


def _in_function_body(node):
    cur = node.parent
    while cur is not None:
        if cur.type == "function_definition":
            return True
        cur = cur.parent
    return False


def _scope_prefix(node, src: bytes) -> str:
    """Qualified-name prefix from enclosing namespaces and classes (e.g. 'ns3::WifiMac')."""
    parts = []
    cur = node.parent
    while cur is not None:
        if cur.type == "namespace_definition" or cur.type in _CLASS_TYPES:
            n = cur.child_by_field_name("name")
            if n is not None:
                parts.append(_text(n, src))
        cur = cur.parent
    return "::".join(reversed(parts))


def _declarator_name_node(fdecl):
    """Descend through declarator wrappers to the actual name node."""
    d = fdecl.child_by_field_name("declarator")
    while d is not None and d.type in (
        "pointer_declarator",
        "reference_declarator",
        "parenthesized_declarator",
        "function_declarator",
    ):
        inner = d.child_by_field_name("declarator")
        if inner is None:
            break
        d = inner
    return d


def _string_text(node, src: bytes) -> str | None:
    """Text of a string_literal / concatenated_string argument, or None."""
    if node.type == "string_literal":
        return "".join(
            _text(c, src) for c in node.children if c.type == "string_content"
        )
    if node.type == "concatenated_string":
        parts = [_string_text(c, src) for c in node.children]
        return "".join(p for p in parts if p is not None)
    return None


def _positional_args(args_node):
    """Children of an argument_list that are actual arguments."""
    return [c for c in args_node.children if c.is_named and c.type != "comment"]


def _enclosing_gettypeid(node, src: bytes):
    """Enclosing function_definition if it is a GetTypeId body, else None."""
    cur = node.parent
    while cur is not None:
        if cur.type == "function_definition":
            d = cur.child_by_field_name("declarator")
            while d is not None and d.type not in ("function_declarator",):
                d = d.child_by_field_name("declarator")
            if d is None:
                return None
            name = d.child_by_field_name("declarator")
            if name is not None and _text(name, src).split("::")[-1] == "GetTypeId":
                return cur
            return None
        cur = cur.parent
    return None


def _attr_type(value_node, checker_node, src: bytes) -> str | None:
    """Derive 'Time', 'Uinteger<uint8_t>', 'Pointer<RandomVariableStream>' etc.
    from the initial-value constructor and the Make*Checker call."""
    base = tmpl = None
    if checker_node is not None:
        m = re.match(r"Make(\w+)Checker(?:<(.+?)>)?\s*\(", _text(checker_node, src))
        if m:
            base, tmpl = m.group(1), m.group(2)
    if base is None and value_node is not None and value_node.type == "call_expression":
        fn = value_node.child_by_field_name("function")
        if fn is not None:
            base = _text(fn, src).removesuffix("Value") or None
    if base is None:
        return None
    return f"{base}<{tmpl}>" if tmpl else base


def extract(src: bytes) -> dict:
    tree = _PARSER.parse(src)
    root = tree.root_node

    symbols: list[dict] = []
    base_classes: list[dict] = []
    includes: list[dict] = []
    calls: list[dict] = []
    typeid_attributes: list[dict] = []
    trace_sources: list[dict] = []
    seen: set[int] = set()

    def add_symbol(node, kind, name, qname, sig, parent):
        symbols.append(
            {
                "tmp": node.id,
                "parent_tmp": parent.id if parent is not None else None,
                "kind": kind,
                "name": name,
                "qualified_name": qname,
                "signature": sig,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "order": node.start_byte,
            }
        )

    # --- classes / structs / enums -------------------------------------------
    for _i, caps in _matches("classes", root):
        if "def.node" in caps:
            node, name_node = caps["def.node"][0], caps["def.name"][0]
            kind = "class" if node.type == "class_specifier" else "struct"
        else:
            node, name_node = caps["enum.node"][0], caps["enum.name"][0]
            kind = "enum"
        if node.id in seen:
            continue
        seen.add(node.id)

        name = _text(name_node, src)
        prefix = _scope_prefix(node, src)
        qname = f"{prefix}::{name}" if prefix else name
        body = node.child_by_field_name("body")
        sig_end = body.start_byte if body is not None else node.end_byte
        sig = _oneline(src[node.start_byte : sig_end].decode("utf8", "replace"))
        add_symbol(node, kind, name, qname, sig, _enclosing_class(node))

        if kind != "enum":
            bc = next(
                (c for c in node.children if c.type == "base_class_clause"), None
            )
            if bc is not None:
                access = None
                for c in bc.children:
                    if c.type == "access_specifier":
                        access = _text(c, src)
                    elif c.type in ("type_identifier", "qualified_identifier"):
                        base_classes.append(
                            {
                                "class_tmp": node.id,
                                "base_name": _text(c, src),
                                "access": access,
                            }
                        )
                    elif c.type == "template_type":
                        tn = c.child_by_field_name("name")
                        base_classes.append(
                            {
                                "class_tmp": node.id,
                                "base_name": _text(tn if tn is not None else c, src),
                                "access": access,
                            }
                        )

    # --- functions and methods -----------------------------------------------
    for _i, caps in _matches("functions", root):
        if "fn.declarator" in caps:
            owner, fdecl, is_def = caps["fn.node"][0], caps["fn.declarator"][0], True
        else:
            owner, fdecl, is_def = caps["proto.node"][0], caps["proto.declarator"][0], False
        if owner.id in seen:
            continue
        if not is_def and _in_function_body(owner):
            continue  # local declarations inside function bodies are noise

        name_node = _declarator_name_node(fdecl)
        if name_node is None:
            continue
        name_text = _text(name_node, src)
        if not name_text:
            continue
        seen.add(owner.id)

        name = name_text.split("::")[-1]
        prefix = _scope_prefix(owner, src)
        qname = f"{prefix}::{name_text}" if prefix else name_text
        parent = _enclosing_class(owner)

        if is_def:
            kind = "method_def" if (parent is not None or "::" in name_text) else "function"
            body = owner.child_by_field_name("body")
            sig_end = body.start_byte if body is not None else owner.end_byte
            sig = _oneline(src[owner.start_byte : sig_end].decode("utf8", "replace"))
        else:
            kind = "method_decl" if parent is not None else "function_decl"
            sig = _oneline(_text(owner, src)).rstrip(";").strip()

        add_symbol(owner, kind, name, qname, sig, parent)

    # --- includes --------------------------------------------------------------
    for _i, caps in _matches("includes", root):
        if "inc.quoted" in caps:
            includes.append(
                {"path": _text(caps["inc.quoted"][0], src).strip('"'), "is_system": 0}
            )
        else:
            includes.append(
                {"path": _text(caps["inc.system"][0], src).strip("<>"), "is_system": 1}
            )

    # --- call-site candidates ---------------------------------------------------
    for _i, caps in _matches("calls", root):
        node = caps["call.node"][0]
        nn = caps.get("call.name") or caps.get("call.qname")
        if not nn:
            continue
        callee = _text(nn[0], src).split("::")[-1]
        if not callee:
            continue
        enc = None
        cur = node.parent
        while cur is not None:
            if cur.type == "function_definition":
                enc = cur.id
                break
            cur = cur.parent
        calls.append(
            {"callee": callee, "line": node.start_point[0] + 1, "enclosing_tmp": enc}
        )

    # --- ns-3 TypeId data (attributes + trace sources) --------------------------
    # First pass: map each GetTypeId function_definition to its TypeId name,
    # taken from the TypeId("ns3::X") constructor call inside it.
    typeid_of_fn: dict[int, str] = {}
    reg_calls: list[tuple] = []
    for _i, caps in _matches("typeid", root):
        if "ctor.name" in caps:
            if _text(caps["ctor.name"][0], src) != "TypeId":
                continue
            fn = _enclosing_gettypeid(caps["ctor.node"][0], src)
            tid_name = _string_text(caps["ctor.string"][0], src)
            if fn is not None and tid_name:
                typeid_of_fn.setdefault(fn.id, tid_name)
        elif "reg.method" in caps:
            method = _text(caps["reg.method"][0], src)
            if method in ("AddAttribute", "AddTraceSource"):
                # Line from the method-name token: the call_expression node of a
                # chained call starts where the whole chain starts.
                reg_calls.append(
                    (method, caps["reg.node"][0], caps["reg.args"][0],
                     caps["reg.method"][0].start_point[0] + 1)
                )
        elif "trace.type" in caps:
            if _text(caps["trace.type"][0], src) != "TracedCallback":
                continue
            node = caps["trace.node"][0]
            cls = _scope_prefix(node, src)
            if not cls:
                continue
            trace_sources.append(
                {
                    "class_name": cls,
                    "trace_name": _text(caps["trace.name"][0], src),
                    "help": None,
                    "callback_type": _oneline(
                        _text(node.child_by_field_name("type"), src)
                    ),
                    "origin": "TracedCallback",
                    "line": node.start_point[0] + 1,
                }
            )

    for method, node, args_node, line in reg_calls:
        fn = _enclosing_gettypeid(node, src)
        if fn is None:
            continue  # AddAttribute outside a GetTypeId body: not TypeId metadata
        cls = typeid_of_fn.get(fn.id)
        if cls is None:
            # Fallback: derive "ns3::X" from the X::GetTypeId declarator scope.
            prefix = _scope_prefix(fn, src)
            d = fn.child_by_field_name("declarator")
            while d is not None and d.type != "function_declarator":
                d = d.child_by_field_name("declarator")
            name = d.child_by_field_name("declarator") if d is not None else None
            qtext = _text(name, src) if name is not None else ""
            owner = qtext.split("::")[:-1]
            parts = [p for p in [prefix] + owner if p]
            if not parts:
                continue
            cls = "::".join(parts)
        args = _positional_args(args_node)
        if method == "AddAttribute" and len(args) >= 3:
            # Overload with TypeId::ATTR_* access flags as 3rd argument:
            # the initial value and accessor/checker shift right by one.
            v = 3 if re.search(r"\bATTR_", _text(args[2], src)) and len(args) >= 4 else 2
            typeid_attributes.append(
                {
                    "class_name": cls,
                    "attr_name": _string_text(args[0], src) or _oneline(_text(args[0], src)),
                    "attr_type": _attr_type(
                        args[v], args[v + 2] if len(args) >= v + 3 else None, src
                    ),
                    "default_value": _oneline(_text(args[v], src)),
                    "help": _string_text(args[1], src),
                    "line": line,
                }
            )
        elif method == "AddTraceSource" and len(args) >= 3:
            trace_sources.append(
                {
                    "class_name": cls,
                    "trace_name": _string_text(args[0], src) or _oneline(_text(args[0], src)),
                    "help": _string_text(args[1], src),
                    "callback_type": _string_text(args[3], src) if len(args) >= 4 else None,
                    "origin": "AddTraceSource",
                    "line": line,
                }
            )

    return {
        "symbols": symbols,
        "base_classes": base_classes,
        "includes": includes,
        "calls": calls,
        "typeid_attributes": typeid_attributes,
        "trace_sources": trace_sources,
    }
