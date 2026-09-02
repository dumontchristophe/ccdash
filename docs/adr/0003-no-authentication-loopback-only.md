# No authentication: loopback bind, Host allowlist, Origin refusal

ccdash binds `127.0.0.1` and has no authentication, none planned: anyone reaching
the port reads everything, and the threat guarded is a browser page talking to
`127.0.0.1`, not a remote attacker. A `Host` allowlist defeats DNS rebinding and
refusing any `Origin` on a POST defeats CSRF;
