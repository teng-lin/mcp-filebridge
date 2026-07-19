COMPOSE = docker compose

.PHONY: help up down logs smoke config setup

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n",$$1,$$2}'

setup: ## Write .env with generated secrets (skips if .env exists)
	@test -f .env && echo ".env exists, leaving it" || { \
	  cp .env.example .env; \
	  python3 -c "import secrets,pathlib,re; p=pathlib.Path('.env'); t=p.read_text(); \
t=re.sub(r'MCP_BEARER_TOKEN=.*','MCP_BEARER_TOKEN='+secrets.token_urlsafe(32),t); \
t=re.sub(r'CONVERTX_JWT_SECRET=.*','CONVERTX_JWT_SECRET='+secrets.token_urlsafe(24),t); \
t=re.sub(r'CONVERTX_PASSWORD=.*','CONVERTX_PASSWORD='+secrets.token_urlsafe(18),t); \
p.write_text(t)"; \
	  echo "wrote .env with generated secrets"; }

up: ## Build and start the stack (local: MinIO + ConvertX + MCP)
	$(COMPOSE) up -d --build

down: ## Stop and remove the stack (keeps volumes)
	$(COMPOSE) down

logs: ## Tail the MCP server logs
	$(COMPOSE) logs -f convertx-mcp

config: ## Validate the merged compose config
	$(COMPOSE) config

smoke: ## Round-trip a file end to end (inside the compose network)
	$(COMPOSE) exec -e S3_PUBLIC_ENDPOINT=http://minio:9000 \
	  convertx-mcp python examples/convertx/server.py --smoke
