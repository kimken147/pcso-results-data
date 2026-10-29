"""Marks the pipeline root for pytest so `pytest -q` can import the module under test
(pytest only puts the rootdir on sys.path when a conftest.py lives here)."""
