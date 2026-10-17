# Sinan

Sinan is an AI travel planning application with React and a single FastAPI backend.
A constrained LangGraph workflow creates daily itineraries, streams progress, and presents edits for confirmation.
Estimated facts require verification before departure.

## Try the fixed demo

Requirements: Docker Engine/Desktop with Compose 2.20+. From the repository root:

```bash
docker compose -f compose.setup.yml run --rm setup
docker compose --profile apps up -d --build --wait --wait-timeout 300
```

Open [the local demo](http://localhost:5173/demo). No account or model API key is required.
The demo reuses the latest production pages and animations with synthetic local data. Changes are kept in this tab only; no business API or model is called, and this does not prove model quality.
Public showcase: [sinan-demo.vercel.app](https://sinan-demo.vercel.app). See [Vercel deployment](docs/public/vercel-demo.md).
For small-scale hosting, see the [Railway deployment guide](docs/public/production.md): a same-domain container stack, invite codes, a one-off CNY 6 trial credit, and a minimal admin console. The repository ships deployment preparation only; public-internet and real-model acceptance must be confirmed separately. A platform-free self-hosted alternative (Caddy full stack) is described in [deploy/README.md](deploy/README.md).
Container builds and optional map tiles require internet access.

Initialization preserves existing configuration and generates secrets only for new files.
Diagnostics: `docker compose exec -T agent-python python scripts/preview_doctor.py`.
For real generation, configure a model provider in the backend environment file.

The [Chinese README](README.md) is the primary guide. See [architecture](docs/public/architecture.md),
[configuration](docs/public/configuration.md), [development](docs/public/development.md),
[pre-departure care and travel memory](docs/public/care-and-memory.md),
[troubleshooting](docs/public/troubleshooting.md), and [contributing](.github/CONTRIBUTING.md).
The 2026-10-11 open-source preview acceptance record is archived in [docs/evidence](docs/evidence/preview-acceptance-2026-10-11.md); its numbers describe that historical batch, not the current version.
Use the private channel in [SECURITY.md](.github/SECURITY.md) for vulnerabilities.

Project code is MIT licensed. Photographs and fonts retain separate licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
