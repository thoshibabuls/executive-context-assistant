"""Structured-output schemas, one module per role: ``output_schemas/<role>.py`` (AI_PIPELINE.md §11).

Each schema is a Pydantic model; its JSON schema is sent with the request and the response is
validated against it. Slice 0.4 adds the layout only; the first schema (AI-01) arrives in 1.4.
"""
