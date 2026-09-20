"""Per-call reports. Import from the submodules (``builder``, ``schema``, ``service``).

This package ``__init__`` deliberately imports nothing: the database client depends
on ``schema``, and importing the builder here would pull in ``gen_ai`` — which
imports the database client — and create an import cycle.
"""
