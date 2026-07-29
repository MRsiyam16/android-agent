"""The FastAPI server, split by concern.

`server.py` at the repo root is still the entry point and still exposes everything it
used to; this package is where the implementation actually lives. The split exists so
that editing one endpoint does not mean loading all 1100 lines of the old single file.
"""
