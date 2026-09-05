# ============================================================
#  homelable — operational Makefile
# ============================================================

COMPOSE     := docker compose -f docker-compose.yml -f docker-compose.debug.yml
SCRIPTS     := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))scripts

# ANSI colours
RESET  := \033[0m
BOLD   := \033[1m
RED    := \033[31m
GREEN  := \033[32m
YELLOW := \033[33m
BLUE   := \033[34m
CYAN   := \033[36m
WHITE  := \033[97m
DIM    := \033[2m

.DEFAULT_GOAL := help

.PHONY: help up up-detached down restart pull deploy \
        logs logs-backend logs-frontend logs-mcp \
        logs-unifi logs-opnsense logs-pfsense \
        ps shell-backend shell-mcp \
        db-stats db-query sync-test approve-source force-approve-infra approve-firewalls snmp-enable auto-place sync-xcpng clean

# ── help ─────────────────────────────────────────────────────
help:
	@printf "\n$(BOLD)$(WHITE)  homelable ops$(RESET)  $(DIM)docker compose wrapper$(RESET)\n\n"
	@printf "$(BOLD)  DEPLOY$(RESET)\n"
	@printf "  $(CYAN)%-20s$(RESET)%s\n" "up"          "Start all containers (attached)"
	@printf "  $(CYAN)%-20s$(RESET)%s\n" "up-detached" "Start all containers (detached)"
	@printf "  $(CYAN)%-20s$(RESET)%s\n" "down"        "Stop and remove containers"
	@printf "  $(CYAN)%-20s$(RESET)%s\n" "restart"     "Restart all containers"
	@printf "  $(CYAN)%-20s$(RESET)%s\n" "pull"        "Pull latest debug images from GHCR"
	@printf "  $(CYAN)%-20s$(RESET)%s\n" "deploy"      "Pull latest images then restart"
	@printf "\n$(BOLD)  LOGS$(RESET)\n"
	@printf "  $(YELLOW)%-20s$(RESET)%s\n" "logs"          "Tail all container logs"
	@printf "  $(YELLOW)%-20s$(RESET)%s\n" "logs-backend"  "Tail backend logs"
	@printf "  $(YELLOW)%-20s$(RESET)%s\n" "logs-frontend" "Tail frontend logs"
	@printf "  $(YELLOW)%-20s$(RESET)%s\n" "logs-mcp"      "Tail MCP server logs"
	@printf "  $(YELLOW)%-20s$(RESET)%s\n" "logs-unifi"    "Backend logs filtered: UniFi"
	@printf "  $(YELLOW)%-20s$(RESET)%s\n" "logs-opnsense" "Backend logs filtered: OPNsense"
	@printf "  $(YELLOW)%-20s$(RESET)%s\n" "logs-pfsense"  "Backend logs filtered: pfSense"
	@printf "\n$(BOLD)  INSPECT$(RESET)\n"
	@printf "  $(GREEN)%-20s$(RESET)%s\n" "ps"            "Show running container status"
	@printf "  $(GREEN)%-20s$(RESET)%s\n" "shell-backend" "Open shell in backend container"
	@printf "  $(GREEN)%-20s$(RESET)%s\n" "shell-mcp"     "Open shell in MCP container"
	@printf "  $(GREEN)%-20s$(RESET)%s\n" "db-stats"      "Device counts by source + status"
	@printf "  $(GREEN)%-20s$(RESET)%s\n" "db-query"      "Run SQL: make db-query SQL=\"SELECT ...\""
	@printf "\n$(BOLD)  MAINTENANCE$(RESET)\n"
	@printf "  $(RED)%-20s$(RESET)%s\n"   "sync-test"     "Test all integration connections"
	@printf "  $(RED)%-20s$(RESET)%s\n"   "approve-source" "Approve pending devices by source: make approve-source SOURCE=pfsense"
	@printf "  $(RED)%-20s$(RESET)%s\n"   "force-approve-infra" "Force-approve pending switch/AP/router devices (bypasses canvas check)"
	@printf "  $(RED)%-20s$(RESET)%s\n"   "approve-firewalls" "Approve + retype pending opnsense/pfsense/vyos as firewall (t0 in layout)"
	@printf "  $(RED)%-20s$(RESET)%s\n"   "snmp-enable"    "Enable SNMP on all approved devices (SNMP_ENABLED=false to disable)"
	@printf "  $(RED)%-20s$(RESET)%s\n"   "auto-place"     "Run topology auto-place layout (FORCE=true to reposition, DESIGN_ID=<id>)"
	@printf "  $(RED)%-20s$(RESET)%s\n"   "sync-xcpng"    "Trigger immediate XCP-ng VM inventory sync"
	@printf "  $(RED)%-20s$(RESET)%s\n"   "clean"         "Stop + remove volumes (DESTRUCTIVE)"
	@printf "\n"

# ── deploy ───────────────────────────────────────────────────
up:
	@printf "$(BOLD)$(BLUE)══ Starting homelable ──────────────────────────────$(RESET)\n"
	@$(COMPOSE) up

up-detached:
	@printf "$(BOLD)$(BLUE)══ Starting homelable (detached) ───────────────────$(RESET)\n"
	@$(COMPOSE) up -d
	@printf "$(GREEN)  ✓ Services started$(RESET)\n"
	@$(COMPOSE) ps

down:
	@printf "$(BOLD)$(BLUE)══ Stopping homelable ──────────────────────────────$(RESET)\n"
	@$(COMPOSE) down
	@printf "$(GREEN)  ✓ Stopped$(RESET)\n"

restart:
	@printf "$(BOLD)$(BLUE)══ Restarting homelable ────────────────────────────$(RESET)\n"
	@$(COMPOSE) restart
	@printf "$(GREEN)  ✓ Restarted$(RESET)\n"

pull:
	@printf "$(BOLD)$(BLUE)══ Pulling latest images ───────────────────────────$(RESET)\n"
	@printf "$(CYAN)  → Fetching from ghcr.io/slmingol ...$(RESET)\n"
	@$(COMPOSE) pull
	@printf "$(GREEN)  ✓ Images up to date$(RESET)\n"

deploy: pull
	@printf "$(BOLD)$(BLUE)══ Deploying ────────────────────────────────────────$(RESET)\n"
	@printf "$(CYAN)  → Restarting with new images ...$(RESET)\n"
	@$(COMPOSE) up -d --pull always
	@printf "$(GREEN)  ✓ Deployed$(RESET)\n"
	@$(COMPOSE) ps

# ── logs ─────────────────────────────────────────────────────
logs:
	@printf "$(BOLD)$(YELLOW)══ All logs ────────────────────────────────────────$(RESET)\n"
	@$(COMPOSE) logs -f

logs-backend:
	@printf "$(BOLD)$(YELLOW)══ Backend logs ────────────────────────────────────$(RESET)\n"
	@$(COMPOSE) logs -f backend

logs-frontend:
	@printf "$(BOLD)$(YELLOW)══ Frontend logs ───────────────────────────────────$(RESET)\n"
	@$(COMPOSE) logs -f frontend

logs-mcp:
	@printf "$(BOLD)$(YELLOW)══ MCP logs ────────────────────────────────────────$(RESET)\n"
	@$(COMPOSE) logs -f mcp

logs-unifi:
	@printf "$(BOLD)$(YELLOW)══ UniFi sync logs ─────────────────────────────────$(RESET)\n"
	@$(COMPOSE) logs -f backend 2>&1 | grep --line-buffered -i unifi

logs-opnsense:
	@printf "$(BOLD)$(YELLOW)══ OPNsense sync logs ──────────────────────────────$(RESET)\n"
	@$(COMPOSE) logs -f backend 2>&1 | grep --line-buffered -i opnsense

logs-pfsense:
	@printf "$(BOLD)$(YELLOW)══ pfSense sync logs ───────────────────────────────$(RESET)\n"
	@$(COMPOSE) logs -f backend 2>&1 | grep --line-buffered -i pfsense

# ── inspect ──────────────────────────────────────────────────
ps:
	@printf "$(BOLD)$(GREEN)══ Container status ────────────────────────────────$(RESET)\n"
	@$(COMPOSE) ps

shell-backend:
	@printf "$(BOLD)$(GREEN)══ Backend shell ───────────────────────────────────$(RESET)\n"
	@$(COMPOSE) exec backend /bin/sh

shell-mcp:
	@printf "$(BOLD)$(GREEN)══ MCP shell ───────────────────────────────────────$(RESET)\n"
	@$(COMPOSE) exec mcp /bin/sh

db-stats:
	@printf "$(BOLD)$(GREEN)══ Database stats ──────────────────────────────────$(RESET)\n"
	@$(COMPOSE) exec -T backend python3 - < $(SCRIPTS)/db_stats.py

db-query:
	@printf "$(BOLD)$(GREEN)══ DB query ────────────────────────────────────────$(RESET)\n"
	@SQL="$(SQL)" $(COMPOSE) exec -T -e SQL backend python3 - < $(SCRIPTS)/db_query.py

# ── maintenance ──────────────────────────────────────────────
sync-test:
	@printf "$(BOLD)$(CYAN)══ Integration connection tests ────────────────────$(RESET)\n"
	@$(COMPOSE) exec -T backend python3 - < $(SCRIPTS)/sync_test.py

SOURCE ?= pfsense
approve-source:
	@printf "$(BOLD)$(RED)══ Approving pending devices from '$(SOURCE)' ───────$(RESET)\n"
	@$(COMPOSE) exec -T -e MCP_SERVICE_KEY backend python3 - $(SOURCE) < $(SCRIPTS)/approve_source.py

force-approve-infra:
	@printf "$(BOLD)$(RED)══ Force-approving pending infra devices ───────────────$(RESET)\n"
	@$(COMPOSE) exec -T -e MCP_SERVICE_KEY backend python3 - < $(SCRIPTS)/force_approve_infra.py

approve-firewalls:
	@printf "$(BOLD)$(RED)══ Approving firewall/router devices (opnsense/pfsense/…) ──$(RESET)\n"
	@$(COMPOSE) exec -T -e MCP_SERVICE_KEY backend python3 - < $(SCRIPTS)/approve_firewalls.py

SNMP_ENABLED ?= true
snmp-enable:
	@printf "$(BOLD)$(CYAN)══ Setting SNMP enabled=$(SNMP_ENABLED) on all approved devices ──$(RESET)\n"
	@$(COMPOSE) exec -T -e MCP_SERVICE_KEY backend python3 - $(SNMP_ENABLED) < $(SCRIPTS)/snmp_enable.py

FORCE ?= false
DESIGN_ID ?=
auto-place:
	@printf "$(BOLD)$(CYAN)══ Auto-place topology (FORCE=$(FORCE)) ────────────────────$(RESET)\n"
	@FORCE=$(FORCE) DESIGN_ID=$(DESIGN_ID) $(COMPOSE) exec -T -e MCP_SERVICE_KEY -e FORCE -e DESIGN_ID backend python3 - < $(SCRIPTS)/auto_place.py

sync-xcpng:
	@printf "$(BOLD)$(CYAN)══ XCP-ng VM sync ──────────────────────────────────$(RESET)\n"
	@$(COMPOSE) exec -T -e MCP_SERVICE_KEY backend python3 - < $(SCRIPTS)/sync_xcpng.py

clean:
	@printf "$(BOLD)$(RED)══ Clean ────────────────────────────────────────────$(RESET)\n"
	@printf "$(RED)$(BOLD)  WARNING: removes all volumes including the database!$(RESET)\n"
	@printf "  Press Ctrl-C to abort, Enter to continue ... "; read _
	@$(COMPOSE) down -v
	@printf "$(GREEN)  ✓ Cleaned$(RESET)\n"
