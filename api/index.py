from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import Handler


class handler(Handler):
    def restore_route(self):
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        path = query.get("route", [""])[0]
        self.path = "/" + path.lstrip("/")

    def do_GET(self):
        self.restore_route()
        super().do_GET()

    def do_POST(self):
        self.restore_route()
        super().do_POST()
