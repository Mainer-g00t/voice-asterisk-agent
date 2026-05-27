Pull the latest code from GitHub and redeploy locally:

1. Run `git pull --ff-only` — if it fails (diverged history), stop and tell the user.
2. Run `docker compose up --build -d` from the repo root to rebuild and restart containers.
3. Run `docker compose logs --tail=30` to show recent output from both services and confirm they started cleanly.

The working directory for this project is /Users/lbenavente/claude/voice-asterisk-agent.
