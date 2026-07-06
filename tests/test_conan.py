"""Tests for the conan `date` OS-tzdb option discriminator (conan.py)."""

from xahaud_scripts.build.conan import _pick_date_tz_option


def _graph(date_options: dict) -> dict:
    """A minimal `conan graph info --format=json` shape with a date node."""
    return {
        "graph": {
            "nodes": {
                "0": {"ref": "conanfile", "name": "xrpld"},
                "1": {
                    "ref": "date/3.0.3#abc123",
                    "name": "date",
                    "options": date_options,
                },
                "2": {
                    "ref": "openssl/3.6.0#def456",
                    "name": "openssl",
                    "options": {"shared": "False"},
                },
            }
        }
    }


def test_picks_tz_db_on_new_recipe():
    graph = _graph(
        {"header_only": "False", "tz_db": "download", "use_system_tz_db": "deprecated"}
    )
    assert _pick_date_tz_option(graph) == ["-o", "date/*:tz_db=system"]


def test_picks_use_system_tz_db_on_old_recipe():
    graph = _graph({"header_only": "False", "use_system_tz_db": "False"})
    assert _pick_date_tz_option(graph) == ["-o", "date/*:use_system_tz_db=True"]


def test_prefers_tz_db_when_both_present():
    # The new recipe carries a deprecated use_system_tz_db alongside tz_db.
    graph = _graph({"tz_db": "download", "use_system_tz_db": "deprecated"})
    assert _pick_date_tz_option(graph) == ["-o", "date/*:tz_db=system"]


def test_no_date_node_returns_empty():
    graph = {"graph": {"nodes": {"0": {"ref": "conanfile", "name": "xrpld"}}}}
    assert _pick_date_tz_option(graph) == []


def test_date_node_without_known_tz_option_returns_empty():
    assert _pick_date_tz_option(_graph({"header_only": "True"})) == []


def test_empty_or_malformed_graph_returns_empty():
    assert _pick_date_tz_option({}) == []
    assert _pick_date_tz_option({"graph": {}}) == []
    assert _pick_date_tz_option({"graph": {"nodes": {}}}) == []
