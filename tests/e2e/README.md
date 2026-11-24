# End-to-end tests

The browser end-to-end suite lives in [`apps/dashboard/e2e`](../../apps/dashboard/e2e) so it can use the
dashboard's Playwright installation. Run it with `make test-e2e` (requires the local AI services:
Ollama, whisper-server and Piper — `./scripts/start.sh` starts them).

It launches an isolated instance (temporary data directory, API :8100, dashboard :3100), uses the
installed Google Chrome with a fake microphone that plays real Piper-synthesised speech, and covers
setup, sign-in, the agent builder and test chat, a live voice conversation, contacts and CSV import, a
simulation campaign through the scheduler, analytics, settings and access control.
