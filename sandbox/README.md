# WAGMI Bench sandbox boundary

`sandbox` contains only the untrusted HTTP-agent boundary. The deterministic
engine, recorder, market pack, pack manifest, scenario labels, and time-rebase
offset remain in the host process. No Docker command in this package bind
mounts the repository, a pack, or `.env`.

## Enforcement model

The lifecycle creates two networks and three containers in this order:

1. an internal, IPv4-only agent network;
2. a non-privileged gateway on the internal and ordinary egress networks;
3. a trusted guard launched on the ordinary network so Docker can bind the
   agent API to host loopback, then attached to the internal network before
   readiness;
4. only after the guard is healthy and its rules verify, the untrusted agent
   joins the already-armed guard namespace.

The agent runs as `65532:65532`, read-only, with all capabilities dropped,
`no-new-privileges`, the default Docker seccomp profile, no Docker logging, no
bind/volume mounts, and only a small `/tmp` tmpfs. The guard initially receives
only the three bootstrap capabilities `CAP_NET_ADMIN`, `CAP_SETGID`, and
`CAP_SETUID`; it installs UID-scoped rules, proves IPv6 is disabled with no
non-loopback interface or route, drops groups and then drops to `65533:65533`
with zero effective capabilities before declaring readiness. The untrusted
agent shares the guard's two-interface namespace, but every new connection by
its fixed UID is intercepted before routing. The ordinary interface exists
only to make Docker's `127.0.0.1` host publication usable; it is not an agent
egress bypass.

New agent TCP and UDP connections can reach only the exact loopback HTTP
service port and the gateway port. Everything else is redirected to local
collectors before being closed. The loopback exception permits the image
healthcheck and no other local port. DNS question names and all other denied
destinations are stored only as typed SHA-256 tokens, so a credential embedded
in a DNS label cannot enter a bundle. Filter acceptance for denied traffic is
limited to the two fixed post-NAT collector ports; it does not depend on Docker
retaining a loopback output interface after `REDIRECT`. The HTTPS CONNECT
gateway accepts an exact manifest hostname on port 443, requires the same exact
TLS SNI, rejects the entire DNS result if any answer is non-public, and
connects to the already-vetted literal address rather than resolving twice.

`DockerSandbox.start()` waits for guard proof, strictly parses root `.env`,
selects only an explicit credential-name allowlist into process memory, and
starts Docker with name-only `--env FIREWORKS_API_KEY` arguments plus a
subprocess environment that is never rendered or logged. No second secret
file is created. A separate `agent_env` map accepts only the reference agent's
known public settings. It cannot override host/port/proxy variables or carry
credential-like names/values; LLM mode is bound to the exact provider domain
and credential-name allowlists, while reckless mode is keyless, deny-all, and
fixed to the blocked `https://data.binance.vision/` probe. It then polls
`/healthz` and runs a final live Docker
inspection before returning a host-loopback base URL plus
`DockerEgressEventSource`. The returned `SandboxHandle` delegates both
`decide()` and `drain_harness_events()`, so the episode loop receives one
turn-scoped runnable object. The IC-6
HTTP adapter owns `/decide`, timeout, retry, and response validation. The event
source snapshots both trusted container logs on every drain, rejects
rotation/prefix drift, and returns a tuple of typed `HarnessEvent` values.

## ISO-2 in-container proof

`DockerEgressProbeRunner` sends one fixed standard-library program to the
running agent with `docker exec --interactive ... python3 -`; it does not mount
or copy the repository, packs, manifest, or `.env`. Before executing, it drains
old gateway/guard facts so they cannot satisfy the new proof. The program
checks allowed Fireworks HTTPS transport and attempts a disallowed HTTPS
hostname, a synthetic followed HTTPS redirect, an encoded DNS query over UDP,
raw IPv4 TCP, raw UDP, and a WebSocket-style CONNECT written over a raw proxy
socket. Its one-line receipt contains only fixed case identifiers and coarse
outcomes. The proof passes only when Fireworks was reachable, it emitted no
block fact, and the trusted collectors emitted exactly one matching
secret-safe `EgressBlocked` payload for every adversarial case, with no extra
or duplicate facts.

The Fireworks request deliberately disables redirects: success proves TLS
reachability to the exact allowlisted hostname, not a mutable provider redirect
chain. Redirect enforcement is exercised separately with a synthetic
standard-library 302 response whose followed destination must be refused by
the gateway. This does not certify redirect behavior in arbitrary third-party
HTTP/WebSocket clients. Any real redirect changes the CONNECT hostname and is
therefore independently checked against the exact allowlist.

Direct TCP can report a successful connection and UDP can report a successful
send because iptables redirects them into the local collectors. Those coarse
outcomes are never treated as proof of blocking; the exact trusted event is
mandatory.

Every object has a random ownership label. Cleanup resolves that label before
removing an exact container or network name; it never uses a wildcard or
recursive host path.

## Minimal build contexts

`docker/gateway`, `docker/guard-base`, and `docker/guard` are independent
deny-by-default contexts. Their `.dockerignore` files transmit only the named
runtime files and Dockerfile. `guard-base` starts from the pinned Python
manifest and installs the exact reviewed `iptables=1.8.11-2` package in a
source-free networked dependency build. Record its resulting local image
digest, then build the source-bearing guard with `--network=none` and
`--pull=false`.

The M3 local receipt used:

```sh
docker build --pull=false \
  -t tradeevolve-guard-base:py312-iptables-20260726 \
  sandbox/docker/guard-base
docker image inspect tradeevolve-guard-base:py312-iptables-20260726 \
  --format '{{.Id}}'
docker build --pull=false --network=none \
  --build-arg GUARD_BASE=tradeevolve-guard-base:py312-iptables-20260726 \
  -t tradeevolve-guard-m3:20260726 sandbox/docker/guard
```

The supported source-context helper requires a named base-image manifest
digest; a missing base fails instead of silently selecting an unpinned tag.
`--network=none` applies to Dockerfile build steps, but BuildKit may still
contact the registry for manifest metadata. An operator requiring a physically
offline build must use a preloaded, registry-free BuildKit/OCI source and
independently bind it to the recorded digest; that path is not claimed here.

## Claims deliberately not made yet

- The current kernel collector proves and records IPv4 TCP, raw-IP TCP, UDP,
  and DNS attempts. IPv6 is removed from the namespace and proved absent, but
  an attempted IPv6 syscall cannot produce an `EgressBlocked` record because
  no IPv6 packet reaches the collector.
- Packet raw sockets are prevented by the agent's zero-capability profile and
  seccomp boundary. The collector cannot observe the denied socket-creation
  syscall itself. Recording those attempts would require a separately reviewed
  seccomp-notify, audit, or eBPF witness.
- The gateway enforces CONNECT hostname, TLS SNI, public address class, and
  DNS-rebinding-safe dialing. It does not decrypt TLS and necessarily trusts
  the explicitly allowlisted provider endpoint.
- Docker Desktop supplies a Linux VM. The preflight proves the Docker server
  state, not the macOS host packet filter. If `CAP_NET_ADMIN`, iptables owner
  matching/REDIRECT, Docker seccomp, namespace sysctls, or the collectors are
  unavailable, startup refuses before the agent joins.
- Disabling Docker logs prevents agent stdout/stderr from becoming an output,
  but a reference agent could still deliberately copy a credential into an
  otherwise valid action comment. The full C4.3 bundle/log/report grep proof
  remains mandatory after an actual episode.
- This package does not prove that an arbitrary agent application exposes only
  `/healthz` and `/decide`; that route-surface assertion belongs to the IC-6
  HTTP-agent implementation and its in-container tests.
