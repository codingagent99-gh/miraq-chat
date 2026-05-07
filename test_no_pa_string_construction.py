"""
test_no_pa_string_construction.py — Grep-lock for pa_* string construction.

Phase 4c invariant: WooCommerce pa_* taxonomy slugs may only appear inside
the two Woo-specific backend files and the lookup builder (which constructs
backend_ref entries). Any other file that tries to *construct* a pa_* string
at runtime means the neutral-key refactor has been bypassed.

The following patterns are forbidden outside the allowed files:
  - f"pa_{...}"    — f-string construction
  - "pa_" + ...    — string concatenation
  - .replace("pa_" — stripping the prefix (a sign of legacy round-trip code)
"""

import os
import re
import pathlib


# ─── Configuration ───

REPO_ROOT = pathlib.Path(__file__).parent

ALLOWED_FILES = {
    # Woo-specific backend files — allowed to construct/manipulate pa_* strings
    "ecommerce/woo_endpoints.py",
    # lookup_builder builds backend_ref containing pa_* taxonomy slugs from raw API data
    "store_loader/lookup_builder.py",
    # This test file itself (explaining the pattern)
    "test_no_pa_string_construction.py",
}

# Pattern: constructing a pa_* string dynamically
FORBIDDEN_PATTERN = re.compile(r'f"pa_\{|\'pa_\'\s*\+|"pa_"\s*\+|\.replace\("pa_"|\.replace\(\'pa_\'')


def _python_files():
    """Yield all .py files in the repo (excluding __pycache__)."""
    for p in sorted(REPO_ROOT.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


def test_no_pa_string_construction_outside_allowed_files():
    """
    No .py file outside the allowed list may construct a pa_* string at runtime.
    """
    violations = []

    for py_file in _python_files():
        relative = py_file.relative_to(REPO_ROOT)
        rel_str = str(relative).replace("\\", "/")  # normalise on Windows too

        if rel_str in ALLOWED_FILES:
            continue

        try:
            source = py_file.read_text(encoding="utf-8")
        except Exception:
            continue  # skip unreadable files

        for lineno, line in enumerate(source.splitlines(), start=1):
            # Skip comment lines
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if FORBIDDEN_PATTERN.search(line):
                violations.append(f"  {rel_str}:{lineno}: {line.rstrip()}")

    assert not violations, (
        "Phase 4c invariant violated — pa_* string construction detected outside allowed files.\n"
        "These files must use neutral catalog keys (e.g. 'color', 'tile-size') and look up\n"
        "pa_* slugs only via CatalogAttribute.backend_ref['taxonomy'].\n\n"
        "Violations:\n" + "\n".join(violations)
    )
