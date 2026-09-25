"""Keep pytest out of the issue #22 fixture project's own tree.

``repo/`` is the fixture project's PR head, checked in as plain files. It
carries that project's own ``tests/conftest.py`` and ``test_*.py`` modules,
which import an ``assistant`` package that is not installed here. They are
review input for the repository-context readers, not tests of prxref, so the
suite never collects them.
"""

collect_ignore = ["repo"]
