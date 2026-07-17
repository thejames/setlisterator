"""Sanity checks for the packaging metadata (catches pyproject typos)."""

import pathlib
import tomllib

import setlist_to_plex
import web


def _pyproject():
    text = pathlib.Path(__file__).with_name("pyproject.toml").read_text()
    return tomllib.loads(text)


def test_console_scripts_point_at_real_callables():
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["setlisterator"] == "setlist_to_plex:main"
    assert scripts["setlisterator-web"] == "web:main"
    assert callable(setlist_to_plex.main)
    assert callable(web.main)


def test_project_name_and_modules():
    data = _pyproject()
    assert data["project"]["name"] == "setlisterator"
    assert set(data["tool"]["setuptools"]["py-modules"]) == {
        "setlist_to_plex", "web"}
