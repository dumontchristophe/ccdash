# One private SQLite connection, guarded by a single lock

Under `ThreadingHTTPServer` many threads reach one SQLite file, so the store keeps
one private connection and one `threading.Lock` that every read and write takes,
with `db_init` holding it across the whole open. This serialises access instead
of pooling connections: for a single local reader the single-writer, no-partial-
read simplicity of WAL is worth more than read concurrency.
