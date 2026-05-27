.PHONY: pull up down logs logs-agent logs-tts logs-llm logs-stt logs-asterisk restart cli shell

# Pull latest code + updated base images
pull:
	git pull --ff-only
	docker compose pull

# Build images and start both services in the background
up:
	docker compose up --build -d

# Stop and remove containers
down:
	docker compose down

# Stream logs from all services — last 50 lines per service then follow (Ctrl-C to exit)
logs:
	docker compose logs -f --tail=50

# Stream logs from a single service: make logs-agent | logs-tts | logs-llm | logs-stt | logs-asterisk
logs-agent:
	docker compose logs -f --tail=50 agent

logs-tts:
	docker compose logs -f --tail=50 tts

logs-llm:
	docker compose logs -f --tail=50 llm

logs-stt:
	docker compose logs -f --tail=50 stt

logs-asterisk:
	docker compose logs -f --tail=50 asterisk

# Rebuild and restart only the agent (faster than full `make up`)
restart:
	docker compose up --build -d agent

# Open the Asterisk CLI — use `pjsip show endpoints` to check softphone registration
cli:
	docker compose exec asterisk asterisk -rvvv

# Open a shell inside the agent container
shell:
	docker compose exec agent /bin/bash
