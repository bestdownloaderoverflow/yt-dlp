# wireproxy smoke test

This folder contains an isolated smoke test for `wireproxy`. It does not modify
the production Docker Compose files.

The script:

1. Uses an existing WireGuard config.
2. Creates a temporary copy with a local SOCKS5 listener.
3. Starts `wireproxy`.
4. Compares the direct IP address with the proxied IP address.
5. Verifies HTTPS connectivity and prints the idle RSS memory usage.
6. Removes the temporary config, binary, and process on exit.

Run with the default test profile:

```bash
./test/wireproxy_smoke_test.sh
```

Run with another WireGuard profile:

```bash
./test/wireproxy_smoke_test.sh --config /path/to/wg0.conf
```

If `wireproxy` is not installed, the script temporarily builds version
`v1.1.2` using Go. The resulting binary is removed after the test.

