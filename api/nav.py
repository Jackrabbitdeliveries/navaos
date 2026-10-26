"""Site-wide navigation: injects the shared pancake menu (templates/_nav.html)
into every page just before </body>. Read per request, like the pages."""
from pathlib import Path

_NAV = Path(__file__).parent / "templates" / "_nav.html"


def with_nav(html: str) -> str:
    try:
        nav = _NAV.read_text()
    except OSError:
        return html
    i = html.rfind("</body>")
    return html[:i] + nav + html[i:] if i >= 0 else html + nav
