"""Manifest contract + no-core-imports for plugin-mcp."""

import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "plugin_mcp" / "luna-plugin.toml"


def _load():
    with MANIFEST.open("rb") as f:
        return tomllib.load(f)


def test_manifest_identity():
    m = _load()
    assert m["name"] == "plugin-mcp"
    assert m["entry"] == "plugin_mcp"
    assert m["sdk_version"] == "0"
    assert m["license"] == "MIT"
    assert m["db_tables"] == ["plugin_mcp_servers", "plugin_mcp_tools"]
    parts = m["version"].split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_tool_count_matches_declarations():
    m = _load()
    assert m["requires"]["tools"] == len(m["tools"]) == 11
    assert m["requires"]["depends_on"] == ["plugin-vault"]


def test_no_core_imports_in_source():
    for py in (ROOT / "plugin_mcp").rglob("*.py"):
        for line in py.read_text().splitlines():
            s = line.strip()
            assert not s.startswith("import luna."), f"{py.name}: {s}"
            assert not s.startswith("from luna."), f"{py.name}: {s}"
            assert not s.startswith("from luna "), f"{py.name}: {s}"
