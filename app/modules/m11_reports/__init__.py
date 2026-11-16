"""M11 AI report chat (spec 6/M11, 7.4, Appendix F). The AI sees only the field catalog, never a row.
Pipeline: catalog -> AI proposes FilterSpecs -> Pydantic and the catalog validate -> the compiler builds a
parameterized query with ``org_id`` forced -> read-only role runs it -> CSV or PDF."""
