"""Executive Context Assistant backend.

Modular monolith (BACKEND_DESIGN.md §4-§5). Domain modules import each other only through
their package root (``eca.<module>``); ``eca.platform`` is the shared base layer and
``eca.api`` composes HTTP routers.
"""
