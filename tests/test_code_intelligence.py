from scrappy_forge.code_intelligence import (
    CodeGraph,
    LspFact,
    PythonCodeIndexer,
    SymbolKind,
    attach_to_world,
)


class FakeLsp:
    def facts(self, graph: CodeGraph):
        caller = next(s for s in graph.symbols.values() if s.name == "run")
        target = next(s for s in graph.symbols.values() if s.name == "helper")
        return [LspFact(caller.id, "references", target.id, 0.95, {"server": "fake"})]


def build_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "util.py").write_text("def helper(value):\n    return value + 1\n")
    (root / "service.py").write_text(
        "from util import helper\n\n"
        "class Service:\n"
        "    def compute(self, value):\n"
        "        return helper(value)\n\n"
        "def run(value):\n"
        "    return helper(value)\n"
    )
    return root


def test_python_ast_indexes_modules_classes_functions_and_methods(tmp_path):
    graph = PythonCodeIndexer(build_repo(tmp_path)).build()
    kinds = {symbol.kind for symbol in graph.symbols.values()}
    assert {SymbolKind.MODULE, SymbolKind.CLASS, SymbolKind.FUNCTION, SymbolKind.METHOD} <= kinds
    compute = next(symbol for symbol in graph.symbols.values() if symbol.name == "compute")
    assert compute.signature == "compute(self, value)"
    assert compute.path == "service.py"
    assert compute.source_hash


def test_dependency_edges_include_local_imports_and_unambiguous_calls(tmp_path):
    graph = PythonCodeIndexer(build_repo(tmp_path)).build()
    relations = {edge.relation for edge in graph.edges}
    assert "imports" in relations
    assert "calls" in relations
    helper = next(symbol for symbol in graph.symbols.values() if symbol.name == "helper")
    callers = {
        graph.symbols[edge.source].name
        for edge in graph.edges
        if edge.relation == "calls" and edge.target == helper.id
    }
    assert {"compute", "run"} <= callers


def test_semantic_neighborhood_walks_code_relationships(tmp_path):
    graph = PythonCodeIndexer(build_repo(tmp_path)).build()
    run = next(symbol for symbol in graph.symbols.values() if symbol.name == "run")
    hood = graph.neighborhood(run.id, depth=1)
    names = {row["name"] for row in hood["symbols"]}
    assert "run" in names
    assert "helper" in names
    assert any(edge["relation"] == "calls" for edge in hood["edges"])


def test_optional_lsp_adapter_adds_observation_edges_without_authority(tmp_path):
    graph = PythonCodeIndexer(build_repo(tmp_path)).build(lsp=FakeLsp())
    lsp_edges = [edge for edge in graph.edges if edge.metadata.get("source") == "lsp"]
    assert len(lsp_edges) == 1
    assert lsp_edges[0].relation == "references"
    exported = graph.to_dict()
    assert exported["untrusted_observation"] is True
    assert "permission" not in exported
    assert "authorization" not in exported


def test_parse_errors_are_diagnostics_not_fatal(tmp_path):
    root = build_repo(tmp_path)
    (root / "broken.py").write_text("def nope(:\n")
    graph = PythonCodeIndexer(root).build()
    assert any(row["path"] == "broken.py" and row["kind"] == "parse_error" for row in graph.diagnostics)
    assert any(symbol.name == "helper" for symbol in graph.symbols.values())


def test_attach_to_world_preserves_existing_world_graph(tmp_path):
    graph = PythonCodeIndexer(build_repo(tmp_path)).build()
    world = {
        "schema_version": 1,
        "nodes": [{"id": "file:1", "kind": "file", "attributes": {"path": "service.py"}}],
        "edges": [],
    }
    enriched = attach_to_world(world, graph)
    assert enriched["nodes"] == world["nodes"]
    assert enriched["edges"] == world["edges"]
    assert enriched["code_intelligence"]["root_hash"] == graph.root_hash
    assert any(row["path"] == "service.py" for row in enriched["code_intelligence"]["file_correlations"])


def test_root_hash_changes_when_source_changes(tmp_path):
    root = build_repo(tmp_path)
    first = PythonCodeIndexer(root).build().root_hash
    (root / "util.py").write_text("def helper(value):\n    return value + 2\n")
    second = PythonCodeIndexer(root).build().root_hash
    assert first != second
