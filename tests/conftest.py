import base64
import io

import pytest
from PIL import Image


def make_png(width, height):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def fake_png():
    return make_png


@pytest.fixture(autouse=True)
def _owner_state_in_tmp(tmp_path, monkeypatch):
    """Daemon tests must never write the real runtime dir's owner state."""
    from browser_harness import daemon
    monkeypatch.setattr(daemon, "STATE", tmp_path / "owners.json")
