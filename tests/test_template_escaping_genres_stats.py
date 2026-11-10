"""Static checks: genre/stats page scripts escape data before writing it as HTML."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"


def _read(rel):
    return (TEMPLATES / rel).read_text(encoding="utf-8")


def _body(src, header):
    """Return the brace-balanced body that follows the first occurrence of header."""
    start = src.index(header)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError(f"unbalanced body after {header!r}")


# `${book.x}` / `${d.x}` straight into markup; `${book.x ? ...}` conditionals are fine.
RAW_BOOK = re.compile(r"\$\{\s*book\.\w+\b(?!\s*\?)")
RAW_NODE = re.compile(r"\$\{\s*d\.\w+\b(?!\s*\?)")


def test_genre_search_query_is_json_encoded():
    html = _read("genres/search.html")
    assert "'{{ query | safe }}'" not in html
    assert re.search(r"\{\{\s*query\s*\|\s*tojson\s*\}\}", html)


def test_genre_merge_escapes_category_data():
    html = _read("genres/merge.html")
    data_line = next(l for l in html.splitlines() if "categories_json" in l)
    assert "replace('<'" in data_line, "embedded JSON must neutralise </script>"
    raw = re.compile(r"\$\{\s*(category|source|target)\.(name|description|icon|color|aliases)\b(?!\s*(\?|&&)|\.length)")
    # Only these build innerHTML; the merge confirm() text further down is plain text.
    for fn in ("function updateSourcePreview(", "function updateTargetPreview(",
               "function updateMergePreview("):
        hits = raw.findall(_body(html, fn))
        assert not hits, (fn, hits)


def test_genre_hierarchy_highlight_escapes_term_and_text():
    html = _read("genres/hierarchy.html")
    body = _body(html, "function highlightSearchTerm(")
    assert "new RegExp(`(${term})`" not in body
    assert "escapeRegExp(term)" in body
    assert "escapeHtml(" in body
    assert "function escapeRegExp(" in html
    assert '"${searchTerm}"' not in html


def test_genre_index_bulk_delete_escapes_names():
    body = _body(_read("genres/index.html"), "function bulkDeleteCategories(")
    assert "${categoryName}" not in body
    assert "escapeHtml(categoryName)" in body


def test_library_journey_modals_escape_book_fields():
    html = _read("stats/library_journey.html")
    for fn in ("function generateBookModalContent(", "function showClusterModal("):
        hits = RAW_BOOK.findall(_body(html, fn))
        assert not hits, (fn, hits)


def test_reading_journey_details_escape_log_fields():
    html = _read("stats/reading_journey.html")
    raw_read = re.compile(r"=\s*\w+\.dataset\.(bookTitle|bookAuthors|notes|pagesCurrent|pagesTotal"
                          r"|progressPercent|startPage|endPage|clusterCount)\b")
    for fn in ("function showLogDetails(", "function showClusterDetails("):
        hits = raw_read.findall(_body(html, fn))
        assert not hits, (fn, hits)


def test_reading_journey_script_blocks_parse():
    """A <script> runs until the next </script>, so stray </style> or <script> inside breaks the page."""
    html = _read("stats/reading_journey.html")
    for m in re.finditer(r"<script[^>]*>", html):
        end = html.index("</script", m.end())
        inner = html[m.end():end]
        assert "</style" not in inner and "<script" not in inner, html[:m.start()].count("\n") + 1


def test_reading_journey_click_handlers_bound_once():
    html = _read("stats/reading_journey.html")
    assert 'onclick="showLogDetails(' not in html
    assert 'onclick="showClusterDetails(' not in html
    assert html.count("querySelectorAll('.calendar-cluster')") == 1


def test_network_explorer_tooltip_escapes_and_slider_guarded():
    html = _read("stats/network_explorer.html")
    hits = RAW_NODE.findall(_body(html, "showTooltip(event, d) {"))
    assert not hits, hits
    assert re.search(r"if\s*\(\s*densitySlider\b", _body(html, "function setupFilterControls("))
