from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from .util import ForgeError, encoded, sha
from .workspace import excluded


class SymbolKind(str, Enum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"


@dataclass(frozen=True)
class CodeSymbol:
    id: str
    kind: SymbolKind
    name: str
    qualified_name: str
    path: str
    line: int
    end_line: int
    signature: str | None = None
    parent_id: str | None = None
    source_hash: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        value = asdict(self)
        value["kind"] = self.kind.value
        return value


@dataclass(frozen=True)
class CodeEdge:
    source: str
    relation: str
    target: str
    confidence: float = 1.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LspFact:
    source_symbol: str
    relation: str
    target_symbol: str
    confidence: float = 1.0
    metadata: dict = field(default_factory=dict)


class LspAdapter(Protocol):
    def facts(self, graph: "CodeGraph") -> list[LspFact]: ...


class CodeGraph:
    """Read-only code intelligence graph.

    The graph is an observation derived from workspace contents. It cannot authorize
    edits, commands or external actions; controller policy remains authoritative.
    """

    def __init__(self, project: str, *, root_hash: str):
        self.project = project
        self.root_hash = root_hash
        self.symbols: dict[str, CodeSymbol] = {}
        self.edges: list[CodeEdge] = []
        self.diagnostics: list[dict] = []

    def add_symbol(self, symbol: CodeSymbol) -> None:
        if symbol.id in self.symbols:
            raise ForgeError(f"Duplicate code symbol: {symbol.qualified_name}")
        self.symbols[symbol.id] = symbol

    def add_edge(self, source: str, relation: str, target: str, *, confidence=1.0, metadata=None) -> None:
        if source not in self.symbols or target not in self.symbols:
            return
        edge = CodeEdge(source, relation, target, float(confidence), metadata or {})
        if edge not in self.edges:
            self.edges.append(edge)

    def find(self, text: str, *, limit: int = 20) -> list[CodeSymbol]:
        if not isinstance(text, str) or len(text) > 300 or not 1 <= limit <= 100:
            raise ForgeError("Invalid code graph query")
        needle = text.lower().strip()
        rows = list(self.symbols.values())
        if needle:
            rows = [
                row
                for row in rows
                if needle in row.name.lower()
                or needle in row.qualified_name.lower()
                or needle in row.path.lower()
            ]
        rows.sort(key=lambda row: (row.path, row.line, row.qualified_name))
        return rows[:limit]

    def neighborhood(self, symbol_id: str, *, depth: int = 1, limit: int = 100) -> dict:
        if symbol_id not in self.symbols:
            raise ForgeError("Unknown code symbol")
        if not 0 <= depth <= 4 or not 1 <= limit <= 500:
            raise ForgeError("Invalid code neighborhood bounds")
        seen = {symbol_id}
        frontier = {symbol_id}
        selected_edges: list[CodeEdge] = []
        for _ in range(depth):
            next_frontier = set()
            for edge in self.edges:
                if edge.source in frontier or edge.target in frontier:
                    selected_edges.append(edge)
                    next_frontier.update((edge.source, edge.target))
            next_frontier -= seen
            seen.update(next_frontier)
            frontier = next_frontier
            if not frontier or len(seen) >= limit:
                break
        ordered = sorted(seen, key=lambda sid: (self.symbols[sid].path, self.symbols[sid].line, sid))[:limit]
        allowed = set(ordered)
        return {
            "symbols": [self.symbols[sid].to_dict() for sid in ordered],
            "edges": [e.to_dict() for e in selected_edges if e.source in allowed and e.target in allowed],
        }

    def apply_lsp(self, adapter: LspAdapter) -> int:
        applied = 0
        for fact in adapter.facts(self):
            if fact.source_symbol not in self.symbols or fact.target_symbol not in self.symbols:
                continue
            self.add_edge(
                fact.source_symbol,
                fact.relation,
                fact.target_symbol,
                confidence=fact.confidence,
                metadata={**fact.metadata, "source": "lsp"},
            )
            applied += 1
        return applied

    def to_dict(self) -> dict:
        return {
            "schema_version": 1,
            "project": self.project,
            "root_hash": self.root_hash,
            "symbols": [self.symbols[key].to_dict() for key in sorted(self.symbols)],
            "edges": [edge.to_dict() for edge in self.edges],
            "diagnostics": list(self.diagnostics),
            "untrusted_observation": True,
        }


class _PythonVisitor(ast.NodeVisitor):
    def __init__(
        self, indexer: "PythonCodeIndexer", graph: CodeGraph, path: str, module_id: str, source_hash: str
    ):
        self.indexer = indexer
        self.graph = graph
        self.path = path
        self.module_id = module_id
        self.source_hash = source_hash
        self.scope: list[CodeSymbol] = []
        self.imports: list[tuple[str, str | None]] = []
        self.calls: list[tuple[str, str]] = []

    @property
    def current(self) -> CodeSymbol | None:
        return self.scope[-1] if self.scope else None

    def _signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        args = [arg.arg for arg in node.args.posonlyargs + node.args.args]
        if node.args.vararg:
            args.append("*" + node.args.vararg.arg)
        args.extend(arg.arg for arg in node.args.kwonlyargs)
        if node.args.kwarg:
            args.append("**" + node.args.kwarg.arg)
        return f"{node.name}({', '.join(args)})"

    def _add_named(self, node, kind: SymbolKind, signature: str | None = None):
        parent = self.current
        module_name = self.indexer.module_name(self.path)
        prefix = parent.qualified_name if parent else module_name
        qualified = f"{prefix}.{node.name}" if prefix else node.name
        sid = self.indexer.symbol_id(self.path, qualified, node.lineno)
        symbol = CodeSymbol(
            id=sid,
            kind=kind,
            name=node.name,
            qualified_name=qualified,
            path=self.path,
            line=node.lineno,
            end_line=getattr(node, "end_lineno", node.lineno),
            signature=signature,
            parent_id=parent.id if parent else self.module_id,
            source_hash=self.source_hash,
        )
        self.graph.add_symbol(symbol)
        self.graph.add_edge(symbol.parent_id, "contains", symbol.id)
        self.scope.append(symbol)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef):
        self._add_named(node, SymbolKind.CLASS)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        kind = (
            SymbolKind.METHOD
            if self.current and self.current.kind == SymbolKind.CLASS
            else SymbolKind.FUNCTION
        )
        self._add_named(node, kind, self._signature(node))

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        kind = SymbolKind.METHOD if self.current and self.current.kind == SymbolKind.CLASS else SymbolKind.FUNCTION
        self._add_named(node, kind, self._signature(node))

    def visit_Import(self, node: ast.Import):
        owner = self.current.id if self.current else self.module_id
        for alias in node.names:
            self.imports.append((owner, alias.name))

    def visit_ImportFrom(self, node: ast.ImportFrom):
        owner = self.current.id if self.current else self.module_id
        if node.module:
            self.imports.append((owner, node.module))

    def visit_Call(self, node: ast.Call):
        owner = self.current.id if self.current else self.module_id
        name = self.indexer.call_name(node.func)
        if name:
            self.calls.append((owner, name))
        self.generic_visit(node)


class PythonCodeIndexer:
    """Deterministic Python AST indexer with bounded dependency inference."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    @staticmethod
    def symbol_id(path: str, qualified_name: str, line: int) -> str:
        return "symbol:" + sha(encoded([path, qualified_name, line]))[:32]

    @staticmethod
    def call_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parts = []
            current = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
                return ".".join(reversed(parts))
        return None

    @staticmethod
    def module_name(path: str) -> str:
        p = Path(path)
        parts = list(p.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    def _python_files(self) -> list[Path]:
        files = []
        for path in self.root.rglob("*.py"):
            try:
                rel = path.relative_to(self.root)
            except ValueError:
                continue
            if not excluded(rel) and path.is_file():
                files.append(path)
        return sorted(files)

    def build(self, *, lsp: LspAdapter | None = None) -> CodeGraph:
        file_records = []
        for path in self._python_files():
            raw = path.read_bytes()
            file_records.append((path, sha(raw), raw))
        root_hash = sha(
            encoded([(str(path.relative_to(self.root)), digest) for path, digest, _ in file_records])
        )
        graph = CodeGraph(str(self.root), root_hash=root_hash)
        deferred_imports: list[tuple[str, str]] = []
        deferred_calls: list[tuple[str, str]] = []

        for path, source_hash, raw in file_records:
            rel = path.relative_to(self.root).as_posix()
            module_name = self.module_name(rel)
            module_id = self.symbol_id(rel, module_name or rel, 1)
            graph.add_symbol(
                CodeSymbol(
                    id=module_id,
                    kind=SymbolKind.MODULE,
                    name=module_name.rsplit(".", 1)[-1] if module_name else rel,
                    qualified_name=module_name or rel,
                    path=rel,
                    line=1,
                    end_line=max(1, raw.count(b"\n") + 1),
                    source_hash=source_hash,
                )
            )
            try:
                tree = ast.parse(raw.decode("utf-8"), filename=rel)
            except (SyntaxError, UnicodeDecodeError) as exc:
                graph.diagnostics.append({"path": rel, "kind": "parse_error", "message": str(exc)[:500]})
                continue
            visitor = _PythonVisitor(self, graph, rel, module_id, source_hash)
            visitor.visit(tree)
            deferred_imports.extend((owner, name) for owner, name in visitor.imports if name)
            deferred_calls.extend(visitor.calls)

        modules = {s.qualified_name: s.id for s in graph.symbols.values() if s.kind == SymbolKind.MODULE}
        by_short_name: dict[str, list[str]] = {}
        for symbol in graph.symbols.values():
            by_short_name.setdefault(symbol.name, []).append(symbol.id)

        for owner, module_name in deferred_imports:
            target = modules.get(module_name)
            if target:
                graph.add_edge(owner, "imports", target, confidence=1.0)

        for owner, called in deferred_calls:
            short = called.rsplit(".", 1)[-1]
            candidates = by_short_name.get(short, [])
            if len(candidates) == 1:
                graph.add_edge(owner, "calls", candidates[0], confidence=0.8, metadata={"observed": called})

        if lsp is not None:
            graph.apply_lsp(lsp)
        return graph


def attach_to_world(snapshot: dict, graph: CodeGraph) -> dict:
    """Attach code intelligence as an explicitly untrusted observation layer.

    Existing world nodes/edges are left untouched so code analysis cannot masquerade
    as OS truth. Consumers can correlate code symbols to world file nodes by path.
    """
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("nodes"), list):
        raise ForgeError("Invalid world snapshot")
    value = dict(snapshot)
    value["code_intelligence"] = graph.to_dict()
    value["code_intelligence"]["file_correlations"] = [
        {"symbol_id": symbol.id, "path": symbol.path}
        for symbol in graph.symbols.values()
        if symbol.kind == SymbolKind.MODULE
    ]
    return value
