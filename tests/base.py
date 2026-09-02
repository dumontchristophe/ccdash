# Run: python3 -m unittest discover -s tests -v  (from repo root)
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ccdash.core import store


def remove_db_files(path):
    """A database file and its -wal/-shm siblings, when they exist."""
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.unlink(p)


class BaseDBTest(unittest.TestCase):
    """Base class isolating each test in a temporary DB.

    store keeps one module-level connection, so tests cannot each open their
    own: the isolation has to happen by pointing that single global at a fresh
    file and closing it afterwards. The -wal and -shm siblings are removed too,
    otherwise a crashed run leaves them behind and the next mkstemp name is the
    only thing keeping the two apart.
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db", prefix="ccdash_test_")
        os.close(fd)
        os.unlink(self.db_path)  # db_init creates the file
        store.db_path = self.db_path
        store.db_init(self.db_path)

    def tearDown(self):
        store.db_close()
        remove_db_files(self.db_path)
