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
This is a synthetic fixed replay, not a live model run. It neither saves user data nor proves model quality.
Container builds and optional map tiles require internet access.

Initialization preserves existing configuration and generates secrets only for new files.
Diagnostics: `docker compose exec -T agent-python python scripts/preview_doctor.py`.
For real generation, configure a model provider in the backend environment file.

The [Chinese README](README.md) is the primary guide. See [architecture](docs/public/architecture.md),
[configuration](docs/public/configuration.md), [development](docs/public/development.md),
[troubleshooting](docs/public/troubleshooting.md), and [contributing](CONTRIBUTING.md).
Use the private channel in [SECURITY.md](SECURITY.md) for vulnerabilities.

Project code is MIT licensed. Photographs and fonts retain separate licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
