"""Help & FAQs (#panel-help in static/index.html, static/js/help.js): every tab, and the admins' Users page, has a
Help section that shows only to people who can open it, so a new tab without help fails here."""
import re
from html.parser import HTMLParser
from pathlib import Path

import users

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


class Sections(HTMLParser):
    """The Help sections: [{id, data-panel, data-group, title}]."""

    def __init__(self):
        super().__init__()
        self.found, self._in_h3 = [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "section" and "help-section" in (a.get("class") or "").split():
            self.found.append({"id": a.get("id"), "panel": a.get("data-panel"), "group": a.get("data-group"), "title": ""})
        elif tag == "h3" and self.found and not self.found[-1]["title"]:
            self._in_h3 = self.found[-1]

    def handle_endtag(self, tag):
        if tag in ("h3", "span"):
            self._in_h3 = None

    def handle_data(self, data):
        if self._in_h3 is not None:
            self._in_h3["title"] += data


def sections():
    parser = Sections()
    parser.feed(INDEX.read_text())
    return parser.found


def test_every_tab_and_the_users_page_has_a_help_section_for_the_people_who_can_open_it():
    by_id = {s["id"]: s for s in sections()}
    for panel in [*users.TABS, users.USERS_PANEL]:
        section = by_id.get(f"help-{panel}")
        assert section, f"no Help section for the {panel} panel (id help-{panel})"
        assert section["panel"] == panel  # shown only to people who can open that panel


def test_each_tab_is_filed_under_its_sidebar_group():
    by_id = {s["id"]: s for s in sections()}
    for panel, group in users.TAB_GROUP.items():
        assert by_id[f"help-{panel}"]["group"] == group


def test_help_sections_have_a_title_and_a_topic_group_and_unique_ids():
    found = sections()
    assert found, "no Help sections"
    assert len({s["id"] for s in found}) == len(found)
    for s in found:
        assert s["id"] and s["id"].startswith("help-"), s
        assert s["title"].strip() and s["group"], s


def test_help_is_in_the_sidebar_for_everyone_and_in_the_top_bar():
    html = INDEX.read_text()
    assert re.search(r'<button class="side-nav-item" data-panel="help" data-always', html)
    assert 'id="btn-help"' in html and '<script src="js/help.js"></script>' in html
    session = (INDEX.parent / "js" / "session.js").read_text()
    assert ':not([data-always])' in session  # Help isn't hidden by access, nor anyone's first tab
