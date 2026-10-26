"""Tests run inside the container (they use the baked LDraw library, indexes and
LeoCAD):

    docker compose exec ldraw-nova-app bash -c \
        "pip install -q --break-system-packages -r /app/web/backend/requirements-dev.txt && cd /app/web/backend && pytest -q"

/data and /config are redirected to temp folders before any app module is imported.
"""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="ldraw-nova-web-tests-"))
os.environ["LDRAW_NOVA_DATA_DIR"] = str(_tmp / "data")
os.environ["LDRAW_NOVA_WEB_CONFIG_DIR"] = str(_tmp / "config")
os.environ["LDRAW_NOVA_GLB_CACHE_DIR"] = str(_tmp / "glb-cache")
os.environ["LDRAW_NOVA_GALLERY_MODELS_DIR"] = str(_tmp / "models-gallery")     # not the image's gallery models
(_tmp / "data" / "output").mkdir(parents=True)
(_tmp / "models-gallery").mkdir()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))          # web/backend
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))          # repo root: leocad_render

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_agent_runtime(monkeypatch):
    import agent
    import toolkit
    async def ready(*args):
        pass
    # Readiness itself has real subprocess and cancellation tests. Other agent
    # tests exercise generation without repeating a complete library scan.
    monkeypatch.setattr(agent, 'prepare_workspace', ready)
    monkeypatch.setattr(toolkit, 'start_preparation', lambda: None)
    monkeypatch.setattr(toolkit, 'shutdown', ready)
    monkeypatch.setattr(agent, "_runs", {})
    monkeypatch.setattr(agent, "_shutting_down", False)
    monkeypatch.setattr(agent, "checkpoint", agent._local_checkpoint)


@pytest.fixture(autouse=True)
def offline_model_details(monkeypatch):
    """Unit tests never depend on live catalogues or send provider credentials."""
    import model_discovery
    async def unavailable(*args, **kwargs):
        raise RuntimeError("Catalogue offline in tests")
    monkeypatch.setattr(model_discovery, "_fetch_json", unavailable)


@pytest.fixture(autouse=True)
def offline_publication_database(monkeypatch):
    import publications
    class Offline:
        def bootstrap_publications(self, _legacy): return []
    monkeypatch.setattr(publications, 'Database', Offline)


@pytest.fixture
def legacy_model():
    """Explicit historical receipt for old API fixtures, never automatic approval."""
    import publications
    from purchasing.service import revision_for
    def register(path):
        record=dict(file=path.name,revision=revision_for(path),enforced=False,
                    verification_quote=None,checked_at=None,expires_at=None)
        publications._atomic_json(publications._record_path(path.name),record)
        return path
    return register


@pytest.fixture
def bridge_shop(monkeypatch):
    """Verified stock fixture for tests using the real five-piece bridge builder."""
    import publications
    import time
    from purchasing import service
    from purchasing.domain import CatalogueElement
    from test_publications import Shop
    shop=Shop()
    shop.index.colors['1'].add('1');shop.index.lego_colors['1'].add('23')
    for n,(part,color,lego) in enumerate([('3003','4','21'),('3003','1','23'),('3022','4','21'),('3022','1','23'),('3032','14','24')]):
        element=str(900000+n)
        shop.index.parts[part].add(part);shop.index.elements[part,color].add(element)
        shop.elements[element]=CatalogueElement(element,part,lego,'Fixture part','available',21,time.time(),100)
    monkeypatch.setattr(service,'Database',lambda:shop)
    monkeypatch.setattr(service,'LegoCatalogue',lambda:shop)
    monkeypatch.setattr(publications,'Database',lambda:shop)
    publications.protect_collection()
    return shop


@pytest.fixture
def data_dir() -> Path:
    return _tmp / "data"


@pytest.fixture
def gallery_dir() -> Path:
    return _tmp / "models-gallery"
