from importlib.metadata import PackageNotFoundError, version as _pkg_version


def plugin_version() -> str:
    """Installed package version, or ``"unknown"`` when unavailable.

    Logged as the first INFO line of each parser's ``parse()`` so a user
    reading the convert output can confirm which install actually ran,
    without having to drop out and ``pip show ofxstatement-revolut``.
    Useful when multiple checkouts or a mix of pip / pipx / system
    installs are in play.

    Resolved at runtime via ``importlib.metadata`` so editable installs
    pick up whatever the last reinstall pinned. Falls back to
    ``"unknown"`` when the distribution metadata is absent (running
    tests directly out of a source tree without ``pip install -e``).
    """
    try:
        return _pkg_version("ofxstatement-revolut")
    except PackageNotFoundError:
        return "unknown"
