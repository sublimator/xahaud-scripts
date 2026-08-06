"""Focused tests for Codecov compare-response interpretation."""

from __future__ import annotations

from xahaud_scripts.codecov import _uncovered_lines


def test_uncovered_lines_uses_codecov_line_state_enum():
    data = {
        "files": [
            {
                "name": {"head": "src/example.cpp"},
                "lines": [
                    {
                        "is_diff": True,
                        "added": True,
                        "coverage": {"head": 0},
                        "number": {"head": 10},
                        "value": "+covered();",
                    },
                    {
                        "is_diff": True,
                        "added": True,
                        "coverage": {"head": 1},
                        "number": {"head": 11},
                        "value": "+missed();",
                    },
                    {
                        "is_diff": True,
                        "added": True,
                        "coverage": {"head": 2},
                        "number": {"head": 12},
                        "value": "+partially_covered();",
                    },
                ],
            }
        ]
    }

    assert _uncovered_lines(data) == [
        ("src/example.cpp", 11, "+missed();"),
        ("src/example.cpp", 12, "+partially_covered();"),
    ]


def test_uncovered_lines_keeps_legacy_partial_values_actionable():
    data = {
        "files": [
            {
                "name": {"head": "src/example.cpp"},
                "lines": [
                    {
                        "is_diff": True,
                        "added": True,
                        "coverage": {"head": {"covered": 1, "total": 2}},
                        "number": {"head": 20},
                        "value": "+branchy();",
                    }
                ],
            }
        ]
    }

    assert _uncovered_lines(data) == [("src/example.cpp", 20, "+branchy();")]
