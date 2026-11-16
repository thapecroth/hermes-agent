"""Dashboard-authenticated commands; no new public listener or auth scheme."""
import importlib.util
from pathlib import Path
import sys

from fastapi import APIRouter
from pydantic import BaseModel, Field

from hermes_constants import get_hermes_home

ROOT = Path(__file__).resolve().parent.parent
NAME = "resource_harness_screen"
if NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(NAME, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = module
    spec.loader.exec_module(module)
from resource_harness_screen.interactive_browser import request

router = APIRouter()


class OpenBrowser(BaseModel):
    url: str = Field(min_length=1, max_length=8192)


@router.get("/browser")
def status():
    return request(get_hermes_home(), "status")


@router.post("/browser/open")
def open_browser(body: OpenBrowser):
    return request(get_hermes_home(), "open", url=body.url)


@router.post("/browser/close")
def close_browser():
    return request(get_hermes_home(), "close")
