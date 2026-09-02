"""Entry point for `python3 -m ccdash`.

A two-line delegate so `server` stays a normal, importable module: the tests
import `server` directly and would otherwise re-trigger this run guard.
"""

from .server import main

if __name__ == "__main__":
    main()
