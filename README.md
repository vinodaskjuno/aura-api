# aura-api

Python FastAPI backend for Aura: RDF/OWL ontology build, SHACL validation, the ReAct
modernization advisor, agent orchestration, and the WebSocket driver used by the
VS Code extension.

## Layout

```
src/          the application package — MUST stay under this repo root
  main.py     FastAPI app; 25 routers, /mock-mcp sub-app, WS advisor
  routers/    HTTP surface        services/   business logic
  agents/     analysis agents     connectors/ repo + source ingestion
  graph/      Neo4j client        database/   DynamoDB client
  advisor/    ReAct advisor + encrypted memory
  scripts/    one-shot ops entrypoints (run as `python -m src.scripts.<name>`)
docker/       backend.Dockerfile + entrypoint.sh (build context = this repo root)
data/repos/   scratch space for cloned repos, created at import time
demo-project/ synthetic polyglot fixture app used to feed the ingestion pipeline
```

**`src/` cannot be flattened into the repo root.** ~350 `from src.…` imports across 92
files require `src` to be an importable top-level package with *this directory* on
`sys.path`. That is why the app runs as `uvicorn src.main:app` / `python -m src.main`
with the cwd set here.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r src/requirements.txt
cp src/credentials.ini.example src/credentials.ini   # optional
# create src/.env with AWS/Bedrock or ANTHROPIC_API_KEY, JWT_SECRET, NEO4J_*
.venv/bin/uvicorn src.main:app --reload --port 8000
```

The SPA (`aura-ui`) proxies to `http://localhost:8000` in dev and is served
same-origin behind nginx in production — the backend does **not** serve static assets.

## Build the image

```bash
podman build --platform linux/amd64 -f docker/backend.Dockerfile -t aura-backend .
```

Built and deployed for real by `aura-infra` (`./deploy.sh backend`), which uses this
repo as the build context.

## Related repos

`aura-ui` (SPA) · `aura-vsix` (VS Code extension) · `aura-infra` (Terraform, compose, deploy)
