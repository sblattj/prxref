"""The config schema and the three documents that describe it must not drift.

A configuration key lives in four places: ``config._DEFAULTS``, the
``config.py`` module docstring, ``.env.example``, and ``docs/env-vars.md``.
Nothing in the runtime reads the last three, so a key added to ``_DEFAULTS``
alone works perfectly and is invisible to everyone who does not read the source
— and a name deleted from ``_DEFAULTS`` keeps being advertised by the docs
forever, sending an operator to set a variable nothing reads.

Both directions are checked, because they fail differently and both fail
silently:

* forward — every key in ``_DEFAULTS`` is documented in all three places;
* backward — no document advertises a ``PRXREF_`` name that is not a key.

The backward check needs a small allowlist, and it is deliberately small: the
legacy environment aliases (DERIVED from ``config._LEGACY_ENV_ALIASES``, so
retiring one retires its exemption with it) and exactly one hand-written name,
``PRXREF_FAIL_ON``, which the docs mention only in order to say it does not
exist.

Every failure message names the offending key AND the file to edit, because
this test's whole audience is someone who has just added a key and does not yet
know which of the four surfaces they missed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from prxref import config


def _repo_root() -> Path:
    """The checkout root, located from the PACKAGE rather than the cwd.

    ``Path.cwd()`` is wrong here: pytest is routinely invoked from a
    subdirectory or from a wrapper elsewhere on disk, and a cwd-relative path
    then reads the wrong tree or none at all — a test that passes by accident
    from the repo root and explodes anywhere else. The fallback covers a
    non-editable install, where the package sits in site-packages and the
    documents only exist beside this file.
    """
    from_package = Path(config.__file__).resolve().parents[2]
    if (from_package / "docs" / "env-vars.md").is_file():
        return from_package
    return Path(__file__).resolve().parents[1]


REPO_ROOT = _repo_root()

# Keyed by the path an operator would edit, because that string goes straight
# into the failure message. The config docstring is a document like the other
# two — it is the table a reader of the source lands on first — but it is read
# from the imported module, not the file, so a stale .pyc could never mask it.
SURFACES: dict[str, str] = {
    "docs/env-vars.md": (REPO_ROOT / "docs" / "env-vars.md").read_text(encoding="utf-8"),
    ".env.example": (REPO_ROOT / ".env.example").read_text(encoding="utf-8"),
    "src/prxref/config.py (module docstring)": config.__doc__ or "",
}

_ENV_NAME_RE = re.compile(r"\bPRXREF_[A-Z0-9_]+\b")

# The one hand-written exemption. docs/env-vars.md contains PRXREF_FAIL_ON in
# the sentence "There is deliberately no `PRXREF_FAIL_ON`" — documenting the
# ABSENCE is the point of that paragraph, so the backward check has to tolerate
# it. test_the_exempt_name_is_documented_as_not_existing keeps the exemption
# honest: it fails the day the docs stop saying "no".
DOCUMENTED_NON_KEYS = frozenset({"PRXREF_FAIL_ON"})


def env_name(key: str) -> str:
    """The environment variable a config key is read from."""
    return config._ENV_PREFIX + key.upper()


def expected_names() -> set[str]:
    return {env_name(key) for key in config._DEFAULTS}


def allowed_names() -> set[str]:
    """Every PRXREF_ name a document may legitimately mention."""
    return (
        expected_names()
        | set(config._LEGACY_ENV_ALIASES.values())
        | DOCUMENTED_NON_KEYS
    )


class TestEveryConfigKeyIsDocumented:
    """Forward: a key in the schema that no document mentions."""

    @pytest.mark.parametrize("surface", sorted(SURFACES))
    @pytest.mark.parametrize("key", sorted(config._DEFAULTS))
    def test_the_key_appears_in_the_document(self, key, surface):
        name = env_name(key)
        assert name in SURFACES[surface], (
            f"{name} (config._DEFAULTS[{key!r}]) is undocumented: add it to "
            f"{surface}"
        )

    def test_the_documented_key_count_matches_the_schema(self):
        """docs/env-vars.md states the total out loud, so the number is a
        claim that can go stale on its own."""
        total = len(config._DEFAULTS)
        assert f"**{total}** configuration keys" in SURFACES["docs/env-vars.md"], (
            f"config._DEFAULTS now holds {total} keys; update the stated count "
            "in docs/env-vars.md"
        )

    def test_the_documented_accepted_name_count_matches_the_schema(self):
        """The second stated total: keys plus legacy aliases."""
        accepted = len(config._DEFAULTS) + len(config._LEGACY_ENV_ALIASES)
        assert f"for {accepted} accepted variable names" in SURFACES["docs/env-vars.md"], (
            f"the schema accepts {accepted} variable names; update the stated "
            "count in docs/env-vars.md"
        )


class TestNoDocumentAdvertisesAnUnknownName:
    """Backward: a document naming a variable the runtime never reads.

    This is the direction that outlives a deletion. A key removed from
    ``_DEFAULTS`` leaves its paragraph behind, and the operator who sets the
    variable it describes gets no error at all — just a setting that does
    nothing.
    """

    @pytest.mark.parametrize("surface", sorted(SURFACES))
    def test_every_name_it_mentions_is_a_real_one(self, surface):
        found = set(_ENV_NAME_RE.findall(SURFACES[surface]))
        strays = sorted(found - allowed_names())
        assert not strays, (
            f"{surface} names {', '.join(strays)}, which "
            f"{'is' if len(strays) == 1 else 'are'} not in config._DEFAULTS: "
            f"either add {'it' if len(strays) == 1 else 'them'} to the schema "
            f"or remove {'it' if len(strays) == 1 else 'them'} from {surface}"
        )

    def test_the_allowlist_stays_one_hand_written_entry(self):
        """An allowlist that grows quietly stops being a check. Growing it is
        allowed — growing it without anyone noticing is not."""
        assert DOCUMENTED_NON_KEYS == frozenset({"PRXREF_FAIL_ON"})

    def test_the_exempt_name_is_documented_as_not_existing(self):
        """The exemption is only legitimate while the docs really are saying
        the variable does NOT exist. Implement it one day and this fails,
        forcing the allowlist entry to be reconsidered rather than inherited.
        """
        assert "no `PRXREF_FAIL_ON`" in SURFACES["docs/env-vars.md"]
        assert "PRXREF_FAIL_ON" not in expected_names()

    def test_every_allowlisted_alias_belongs_to_a_real_key(self):
        """The aliases are derived, so this pins the derivation rather than the
        list: an alias pointing at a key that no longer exists is itself drift.
        """
        assert set(config._LEGACY_ENV_ALIASES) <= set(config._DEFAULTS)


class TestTheDocumentsAreFoundAtAll:
    """Positive controls. Without these, a lookup that silently reads an empty
    string would make every check above pass by finding nothing to object to.
    """

    @pytest.mark.parametrize("surface", sorted(SURFACES))
    def test_the_surface_has_content(self, surface):
        text = SURFACES[surface]
        assert text.strip(), f"{surface} came back empty"
        assert _ENV_NAME_RE.search(text), f"{surface} mentions no PRXREF_ name"

    def test_the_root_is_located_from_the_package_not_the_cwd(self):
        """Pins the mechanism: a refactor to a cwd-relative path fails here,
        rather than passing locally and failing wherever CI happens to cd to.
        """
        assert REPO_ROOT.is_absolute()
        assert REPO_ROOT == Path(config.__file__).resolve().parents[2]
        assert REPO_ROOT == Path(__file__).resolve().parents[1]
