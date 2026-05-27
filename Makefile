.PHONY: pull up down logs restart cli shell

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

# Stream logs from both services (Ctrl-C to exit)
logs:
	docker compose logs -f

# Rebuild and restart only the agent (faster than full `make up`)
restart:
	docker compose up --build -d agent

# Open the Asterisk CLI — use `pjsip show endpoints` to check softphone registration
cli:
	docker compose exec asterisk asterisk -rvvv

# Open a shell inside the agent container
shell:
	docker compose exec agent /bin/bash
