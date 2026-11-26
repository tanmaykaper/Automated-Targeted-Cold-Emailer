from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from outreach.settings import load_settings  # noqa: E402

# Never let a developer's real keys leak into tests.
for var in list(os.environ):
    if var.endswith(("_API_KEY", "_KEY")) or var in ("SENDER_EMAIL", "GMAIL_APP_PASSWORD", "DRY_RUN", "STATE_DIR"):
        os.environ.pop(var, None)
os.environ["DOTENV_DISABLED"] = "1"

APOLLO_HEADER = ("First Name,Last Name,Title,Company Name,Email,Email Status,Primary Email Catch-all Status,"
                 "Primary Email Last Verified At,Person Linkedin Url,Website,City,State,Country,Industry,Keywords,"
                 "Apollo Contact Id")


def apollo_csv(*rows: str) -> str:
    return APOLLO_HEADER + "\n" + "\n".join(rows) + "\n"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr("outreach.settings._load_dotenv", lambda: None)
    return load_settings(state_dir=tmp_path / "state", inputs_dir=tmp_path / "inputs", smtp_probe_enabled=False,
                         min_send_delay_s=0, max_send_delay_s=0, randomize_sends=False)


class FakeSearch:
    def __init__(self, results=None):
        self.results = results or {}
        self.queries = []
        self.s = type("S", (), {"keys": type("K", (), {"serper": "", "tavily": ""})()})()

    def search(self, query, num=8, news=False):
        self.queries.append(query)
        for key, res in self.results.items():
            if key in query:
                return res
        return []
