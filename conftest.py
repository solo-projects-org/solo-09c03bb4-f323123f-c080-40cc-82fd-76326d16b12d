"""Pytest bootstrap: makes the project root importable (app package).

pytest inserts the directory containing this file into sys.path (rootdir
conftest), so `import app...` works both locally and inside the container.
"""
