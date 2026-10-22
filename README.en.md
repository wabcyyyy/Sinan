# Sinan

Sinan helps travelers turn a 2–3 day city break into an editable itinerary draft.
Confirm must-see places, arrival/departure windows and budget scope before planning; review changes before saving them.
It uses React, FastAPI, and a planning worker backed by persistent MySQL tasks.
A constrained LangGraph workflow creates daily itineraries, streams progress, and presents edits for confirmation.
Estimated facts require verification before departure.

## Try it

Open the [online demo](https://sinan-demo.vercel.app). No installation, account, or model API key is required.
It uses production pages with a synthetic Chengdu itinerary. Changes stay in the current tab; no business API or model is called.
The skippable “90秒体验局部修改” guide opens an independent sample. Review a scoped edit beside the original timeline, then confirm to save; mobile views keep the selected day and pending draft. Cost differences are shown only for comparable priced items. See the [workbench screenshots and limits](docs/public/vercel-demo.md).
Map tiles require internet access; the itinerary and navigation links remain available if tiles fail.

| Goal | Start here | Requirements |
| --- | --- | --- |
| Explore the product | [Online demo](https://sinan-demo.vercel.app) | Browser |
| Run the fixed demo locally | [Frontend only](#frontend-only) | Node 22.12+ |
| Run the full application | [Full local stack](#full-local-stack) | Docker; a model key for real planning |
| Change the code | [Development guide, Chinese](docs/public/development.md) | Python 3.12, uv, Node 22.12+, Docker |

A [documented local browser case](docs/public/product-case-20261021.md) distinguishes real-model runs, partial delivery and recovery from the fixed demo. It is functional evidence, not a quality or speed benchmark. There is currently no public real-model trial endpoint.

## Get the code

```bash
git clone https://github.com/wabcyyyy/Sinan.git
cd Sinan
```

Choose one local option below. Each starts from the repository root.

## Frontend only

```bash
cd travel-frontend-react
npm ci
npm run dev
```

Visit `/demo` at the address printed by Vite, usually [localhost:5173/demo](http://localhost:5173/demo).
No Python, database, or model key is needed for this route. Real planning on the normal homepage requires the backend.
Press Ctrl+C to stop. See [Vercel deployment, Chinese](docs/public/vercel-demo.md) for a static showcase build.

Worker dispatch, recovery under load, overload rejection and local fake-model HTTP experiments: [planning worker guide, Chinese](docs/public/planning-worker.md). The experiments use MySQL and real processes with fixed research/content, preserving failures and comparison settings; they do not establish model quality or production capacity.

## Full local stack

Requirements: Docker Engine/Desktop with Compose 2.20+. This starts MySQL, Redis, FastAPI, the planning worker, and the frontend; host Python and Node installations are unnecessary.
From the repository root:

```bash
docker compose -f compose.setup.yml run --rm setup
docker compose --profile apps up -d --build --wait --wait-timeout 300
```

Open [the local demo](http://localhost:5173/demo). Initial builds download images and dependencies.

Initialization preserves existing configuration and generates secrets only for new files.
Diagnostics: `docker compose exec -T agent-python python scripts/preview_doctor.py`.
For real generation, configure the backend model provider, recreate the backend container as described in [configuration, Chinese](docs/public/configuration.md), then register and log in on [the homepage](http://localhost:5173/).
Stop containers while preserving data with `docker compose --profile apps stop`.
Before switching to host development, stop the application containers and keep only MySQL and Redis; see the [development guide](docs/public/development.md).

## Repository map

| Path | Purpose |
| --- | --- |
| [`travel-frontend-react/`](travel-frontend-react/README.md) | React pages, API client, and browser tests |
| [`travel-agent-python/`](travel-agent-python/README.md) | FastAPI, services, Agent, migrations, and backend tests |
| [`contracts/`](contracts/) | Generated cross-client schemas; do not edit manually |
| [`docs/public/`](docs/public/) | Architecture, configuration, development, deployment, and troubleshooting |
| [`deploy/`](deploy/README.md) | Self-hosted deployment configuration and scripts |
| [`scripts/`](scripts/) | Root validation, local launch, and secret checks |
| [`.github/`](.github/) | CI, contribution guidelines, and issue templates |

## Deploy your own instance

- [Vercel showcase](docs/public/vercel-demo.md): frontend-only fixed examples.
- [Railway application](docs/public/production.md): a single application instance, persistent storage, invitations, and trial credits.
- [Caddy self-hosting](deploy/README.md): a single-machine Compose stack with HTTPS.

Deployment guides are in Chinese. Public connectivity and real-model behavior need validation in your deployment environment.

## Development and contributing

The [Chinese README](README.md) is the primary guide. See [architecture](docs/public/architecture.md),
[configuration](docs/public/configuration.md), [development](docs/public/development.md),
[pre-departure care and travel memory](docs/public/care-and-memory.md),
[troubleshooting](docs/public/troubleshooting.md), and [contributing](.github/CONTRIBUTING.md).
Use the private channel in [SECURITY.md](.github/SECURITY.md) for vulnerabilities.

Project code is MIT licensed. Photographs and fonts retain separate licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
