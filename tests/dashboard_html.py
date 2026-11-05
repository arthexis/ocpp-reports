"""Small semantic HTML assertions for rendered dashboard tests (stdlib only)."""
from html.parser import HTMLParser


class DashboardHTML(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "text": "", "parent": self.stack[-1] if self.stack else None}
        self.elements.append(node)
        if tag not in {"input", "meta", "link", "br", "hr", "img", "source"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node["text"] += data

    def find(self, tag=None, **attrs):
        return [node for node in self.elements
                if (tag is None or node["tag"] == tag)
                and all(node["attrs"].get(key.replace("_", "-")) == value
                        for key, value in attrs.items())]

    def status_badges(self):
        return [node for node in self.find("span")
                if "status" in node["attrs"].get("class", "").split()]

    def connector_ids(self):
        return [int(node["text"].strip()) for node in self.find("td", data_label="Conector")
                if node["text"].strip().isdigit()]

    def view_pressed(self, view):
        matches = self.find(data_connector_view=view)
        assert len(matches) == 1, (view, matches)
        return matches[0]["attrs"].get("aria-pressed") == "true"

    def cell_badges(self, label):
        """Status badges within table cells identified by their semantic label."""
        return [node for node in self.status_badges()
                if self._ancestor(node, "td", "data-label", label)]

    @staticmethod
    def _ancestor(node, tag, attribute, value):
        parent = node["parent"]
        while parent is not None:
            if parent["tag"] == tag and parent["attrs"].get(attribute) == value:
                return True
            parent = parent["parent"]
        return False

    def count(self, label):
        """Operational station/connector count, looked up by visible card label."""
        cards = self.find("a")
        matches = [card for card in cards
                   if "operational-card" in card["attrs"].get("class", "").split()
                   and any(child["parent"] is card and child["tag"] == "span"
                           and child["text"].strip().startswith(label)
                           for child in self.elements)]
        assert len(matches) == 1, (label, len(matches))
        numbers = [child["text"].strip() for child in self.elements
                   if child["parent"] is matches[0] and child["tag"] == "strong"]
        assert len(numbers) == 1, (label, numbers)
        return numbers[0]
