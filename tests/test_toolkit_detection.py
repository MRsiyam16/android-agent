"""Toolkit classification, which is what makes learned launch waits transferable.

`wait_for_ui` keys its settle budget on the toolkit rather than the package, so "a Flutter
first frame takes ~12s on this machine" carries to the next Flutter app. Misclassify and the
budget is drawn from the wrong population: a Flutter app judged native gets a native-sized
wait, the first dump comes back empty, and everything downstream reads it as a broken app.
"""
from __future__ import annotations

from conftest import dump, node

from adb_device import detect_toolkit


def test_native_layout_is_identified_by_its_resource_ids():
    # Native Android: nearly every element carries a resource-id.
    xml = dump(*[
        node(**{"resource-id": f"com.example.app:id/item_{i}", "text": f"Item {i}",
                "bounds": f"[0,{i * 100}][1080,{(i + 1) * 100}]"})
        for i in range(12)
    ])
    assert detect_toolkit(xml) == "native"


def test_flutter_semantics_tree_is_identified():
    # Flutter publishes labelled ViewGroups with almost no resource-ids — the inverse shape.
    xml = dump(*[
        node(**{"class": "android.view.ViewGroup", "content-desc": f"Label {i}",
                "bounds": f"[0,{i * 100}][1080,{(i + 1) * 100}]"})
        for i in range(12)
    ])
    assert detect_toolkit(xml) == "flutter"


def test_webview_wins_over_everything_else():
    xml = dump(
        node(**{"class": "android.webkit.WebView", "bounds": "[0,0][1080,2400]"}),
        *[node(**{"content-desc": f"Label {i}"}) for i in range(12)],
    )
    assert detect_toolkit(xml) == "webview"


def test_empty_and_tiny_dumps_are_unknown_not_guessed():
    # A status-bar-only dump right after launch must not be classified confidently — that
    # reading is what a too-short learned budget would be built on.
    assert detect_toolkit("") == "unknown"
    assert detect_toolkit(dump(node(**{"content-desc": "x"}))) == "native"


def test_classification_survives_a_malformed_dump():
    # detect_toolkit is regex-based precisely so a torn dump degrades instead of raising.
    assert detect_toolkit("<hierarchy><node unclosed=") in {"native", "flutter", "unknown"}
