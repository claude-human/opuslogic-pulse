# OpusLogic Pulse

External observability dashboard for the OpusLogic Automation Platform.

Fully in-house stack — no third-party observability vendors. Collector polls every
service, stores time-series in DuckDB, streams live updates over WebSocket, and
renders a tabbed React dashboard matching the OpusLogic visual language.

## Architecture

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│  Collector   │─────▶│   DuckDB     │◀─────│   API + WS   │
│  (FastAPI)   │      │  (timeseries)│      │  (FastAPI)   │
└──────┬───────┘      └──────────────┘      └──────┬───────┘
       │                                            │
       │ probes                                     │ WebSocket
       ▼                                            ▼
  Backend /health                             React/Vite UI
  Temporal gRPC                               (7 tabs)
  Postgres pg_stat_*
  Zitadel /healthz
  Docker socket
  /proc (host)
  Agent heartbeats
```

## Tabs

1. **Overview** — traffic-light view of every service, recent incidents
2. **Backend API** — per-endpoint latency, RPS, error rate, synthetic probes
3. **Temporal** — workflow queue depth, worker health, task latency
4. **Postgres** — connections, slow queries, replication, lock waits
5. **Zitadel** — token issuance, login success rate, API latency
6. **Agents** — enrolled agents, heartbeat gaps, version distribution
7. **Host** — CPU, memory, disk, network on the VPS itself

Each tab has a timeline scrubber for postmortems ("what went wrong when").

## Deployment

Runs alongside OpusLogic on the same VPS with its own docker-compose stack.
Public ingress goes through the platform's **existing** Caddy (at
`/opt/opuslogic/Caddyfile`) — add a `pulse.opuslogic.eu` vhost there that
reverse-proxies to `pulse_frontend:80` on the shared `opuslogic-network`.
Authentication goes through the platform's Zitadel (OIDC / PKCE), so the same
users that can sign into OpusLogic can sign into Pulse, with roles mapped to
Pulse scopes (see `collector/auth.py`).

```bash
# 1. Register the Pulse app in Zitadel (one-time)
ZITADEL_URL=https://auth.opuslogic.eu \
ZITADEL_PAT=<personal-access-token> \
    ./scripts/setup-zitadel-pulse.sh

# 2. Fill in .env with the values the script prints, then:
./scripts/deploy.sh
```

DNS: `pulse.opuslogic.eu` (A) must point to the VPS. AAAA records are optional
but only keep them if the VPS actually listens on IPv6 for port 80 (Let's
Encrypt will try both and fail if AAAA resolves to an unreachable host).

## Status

Scaffold — collector framework, DuckDB storage, WebSocket stream, and Overview
tab wired up with host-metrics probe as proof of the pipeline. Additional probes
and tabs land in subsequent iterations.

## Reaching the dashboard over the tunnel (Block 220)

The public route is gone (OpusLogic Block 219). From the host:

    ssh -L 7501:127.0.0.1:7501 <user>@87.106.91.170

then open `http://localhost:7501/#token=<PULSE_SHARED_TOKEN>` once — the token
(set in `infra/.env`) is kept in the tab's sessionStorage and sent as the bearer
token to the data routes and the WebSocket. Without a token in the tab the
Zitadel login runs as before; `http://localhost:7501` is a registered redirect
URI of the `pulse-frontend` application since Block 220.
