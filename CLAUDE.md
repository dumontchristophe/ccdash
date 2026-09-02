## Conventions

Imports run one way down to `store.py` (`docs/backend.md`). These fail silently
if you miss them:

- Read `db_path`/`verbose` as `store.X`, never `from store import X` — the copy
  stays the default when `main` rebinds it. Lowercase for the same reason.
- Imports inside `ccdash/` are package-relative and name the module, not its
  symbols: `from .core import store`, `from .pages import overview`. The package
  shields a module named after a stdlib one from shadowing it process-wide.
- The image `COPY`s the whole `ccdash/` package, so a new module ships with no
  `Dockerfile` edit.

## Where things are

| Question | File |
|---|---|
| Backend layout, ingest, serve | [`docs/backend.md`](docs/backend.md) |
| Frontend: modules, routing, themes | [`docs/frontend.md`](docs/frontend.md) |
| What each view shows | [`docs/dashboard.md`](docs/dashboard.md) |
| Cost semantics, OTEL variables | [`docs/reference.md`](docs/reference.md) |
| What the database holds, threat model | [`SECURITY.md`](SECURITY.md) |
| Contributing | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## Non-negotiables

- **No runtime dependency, no outbound network.** Python stdlib and ES modules
  only — no pip, npm, manifest, lockfile. `ruff`/`mypy` are CI-pinned dev tools.
- **Never edit `ccdash/web/assets/ccdash.css`** — it is generated from `styles/input.css`.
- **Every payload value goes through `escapeHtml`**; a lookup keyed by one needs
  a `Map`, not an object literal.
- **Never `os.path.join` a request path** — static files come from an allowlist,
  the path is only ever a dict key.
- **`DROP_ATTRS` is the whole of the stripping** — its six keys name the user or
  org; prompts, commands, paths and the raw blob are stored as sent.
- **No authentication, none planned.** Anyone reaching the port reads everything
  stored; it binds `127.0.0.1`.
- **Keep the help banners** — they tell a reader the data is missing (an unset
  `OTEL_LOG_*` flag or `delta` temporality) rather than zero.
