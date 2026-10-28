"""Static asset checks only; no browser or Node process is used."""
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        self.urls.extend(value for key, value in attrs if key in {"href", "src", "action"} and value)


def test_web_html_references_only_relative_urls():
    files = list((ROOT / "web").glob("*.html"))
    assert {p.name for p in files} == {"index.html", "dev-smoke.html"}
    for path in files:
        parser = References(); parser.feed(path.read_text())
        assert parser.urls
        for url in parser.urls:
            parsed = urlsplit(url)
            assert not parsed.scheme and not parsed.netloc and not url.startswith("/"), (path, url)
            if parsed.path:
                assert (path.parent / parsed.path).is_file()
        assert not re.search(r"https?://|//cdn", path.read_text())


def test_scripts_and_styles_do_not_load_external_assets():
    for path in list((ROOT / "web").glob("*.mjs")) + list((ROOT / "web").glob("*.css")):
        content = path.read_text()
        assert not re.search(r"https?://|//cdn|@import", content)


def test_sample_map_dimensions_match_workbooks():
    # The tiny static sample manifest must describe the entire used range accurately.
    from sheetaudit.loader import load
    content = (ROOT / "web/app.mjs").read_text()
    for path in (ROOT / "samples").glob("*.xlsx"):
        for sheet in load(path).sheets.values():
            assert f"name: '{sheet.name}', max_row: {sheet.max_row}, max_column: {sheet.max_column}" in content
