# Security

`ccdash` is an OTLP receiver for one developer's own machine. It has no
authentication, and its database holds the text of your prompts and the shell
commands your sessions ran. Read this before you install it, and again before
you move the port off `127.0.0.1`.

## What the database holds

Everything Claude Code exports, minus six identity attributes. Depending on the
env flags you set, that includes:

- **Prompt text**, in clear, when `OTEL_LOG_USER_PROMPTS=1` (off by default).
- **Assistant responses**, in clear, when `OTEL_LOG_ASSISTANT_RESPONSES=1` (off
  by default).
- **Shell commands as run**, when `OTEL_LOG_TOOL_DETAILS=1` — including any
  secret typed on a command line.
- **Absolute file paths**, naming your home directory and every repository.
- **The raw attribute blob** of each event, stored in the `event_attrs` table
  (zlib-compressed, but neither redacted nor truncated).

Ingestion strips exactly six OTLP attributes, the whole of `DROP_ATTRS`:
`user.email`, `user.id`, `user.account_uuid`, `user.account_id`,
`organization.id`, `user.groups`. Nothing else is removed, redacted or
truncated.

**Stripping identity is not anonymising content.** Those six keys name an
account; removing them stops the database carrying your identity as data. It
does nothing to the text. A prompt names whatever you typed — a codebase, a
customer, a person. Treat a copy of the database the way you would treat a copy
of your shell history.

## Where the database lives

`~/.ccdash/ccdash.db` by default, `data/ccdash.db` under the shipped
`compose.yml`, or wherever `--db` points — plus the SQLite sidecars
`ccdash.db-wal` and `ccdash.db-shm`.

The files are narrowed to `0600` at every start-up, and the directory to `0700`
when `ccdash` created it (a pre-existing directory keeps its mode, since `--db`
may name one you use for something else). The pass only clears bits, never adds
them. The OS creates the directory and SQLite the files under your `umask`
first, so there is a window at first start where they are as open as your
`umask` leaves them.

**There is no encryption at rest.** The file is as readable as any other in your
home directory. Keep it out of synced directories, cloud backups and
repositories — a `~/.ccdash` inside Dropbox or iCloud is your prompt history
uploaded to a third party.

## No authentication, on purpose

Anyone who can reach the port reads every stored prompt and command, and can
write to the database. There are no accounts, tokens or access control.

This is a boundary, not an oversight. The interface `ccdash` binds is the access
control: `127.0.0.1` by default, so the trust boundary is "processes on this
host". Moving the bind is the operator's decision — point `--host` at a LAN or
VPN address and you extend that boundary to everyone who can route to it. Give
the server one address, never `0.0.0.0`, and never an internet-reachable
interface. Under Docker the process binds every interface by design and the
narrowing is done by the port publication: `CCDASH_BIND` in `.env` is the
address the port is published on, defaulting to `127.0.0.1`.

## What is defended

- **Cross-origin POST.** A POST carrying an `Origin` header is refused with
  `403`. An exporter never sends one; a browser always does and cannot remove
  it, so a page you visit cannot seed your database.
- **`Host` mismatch on GET.** A GET whose `Host` is not one the server serves
  under gets a `403` — this stops DNS rebinding. The allowlist is the loopback
  names, the bound address, and whatever `--allow-host` or `CCDASH_ALLOW_HOST`
  declares; the server prints it on start-up.
- **Body and decompression ceilings.** A body above 32 MB is rejected with a
  `413` (including a chunked one that grows past it); decompression stops at
  128 MB with a `400`, so a gzip bomb is not inflated into memory.
- **Protobuf refused.** A `Content-Type` naming protobuf gets a `415`: only the
  `http/json` OTLP protocol is decoded.
- **Parameterised SQL.** Every query binds its values; no request input is
  interpolated into a statement.
- **An asset allowlist.** Static files are served from a fixed dict, and the
  request path is only ever a key into it — no filesystem path is built from
  client input.
- **Escaping on every payload value.** The frontend passes every rendered value
  through `escapeHtml`, including the ones that look safe.
- **Tracebacks off the wire.** A failing request returns a status and a generic
  body; the traceback goes to the operator's stderr.

## What is not defended

- **Anything running as you on this machine.** The `0600` mode stops other
  users, not your own processes.
- **Anyone on a network you expose the port to.** Off loopback, everyone who can
  route to the address has full read and write access.
- **The database at rest.** No encryption, no integrity check.
- **A hostile exporter.** Anything that can POST can write rows: fabricated
  sessions, fabricated costs. The dashboard shows what it was sent.

## No outbound network

The server makes no external call and the frontend loads nothing remote — no
CDN, font, analytics or update check. This is checkable, not a claim to trust:
`ccdash/` is Python standard library only and the UI is ES modules served as
written. There is no `requirements.txt`, `package.json` or lockfile, so there is
no third-party runtime code to audit.

## Reporting a vulnerability

Report privately through GitHub: **Security → Report a vulnerability** on
[this repository](https://github.com/dumontchristophe/ccdash/security/advisories/new).
Please do not open a public issue for a security bug.

Include what you were able to do, the request or payload that did it, and the
version or commit you tested. Expect an acknowledgement within a week. `ccdash`
is maintained by one person in their own time: no SLA, no bounty, and a fix
ships when it is ready.

**Out of scope**, being documented design rather than defects: the absence of
authentication, the absence of encryption at rest, and anything requiring access
to the port after an operator has deliberately bound it off loopback.
