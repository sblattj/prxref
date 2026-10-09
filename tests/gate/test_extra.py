"""The github-app extra: PyJWT is optional and its absence is named (#102)."""

import builtins
import sys

import pytest

from prxref import gate


def test_require_jwt_returns_the_module_when_installed():
    jwt = gate.require_jwt()
    assert hasattr(jwt, "encode")


def test_require_jwt_names_the_extra_when_missing(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "jwt" or name.startswith("jwt."):
            raise ImportError("No module named 'jwt'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "jwt", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(gate.GateDependencyError) as info:
        gate.require_jwt()
    assert "github-app" in str(info.value)
    assert isinstance(info.value, ImportError)


def test_importing_the_package_does_not_import_jwt(monkeypatch):
    monkeypatch.delitem(sys.modules, "prxref.gate", raising=False)
    monkeypatch.delitem(sys.modules, "jwt", raising=False)
    __import__("prxref.gate")
    assert "jwt" not in sys.modules
