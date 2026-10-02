# Agent Pipe uploader image

Python/FastMCP image for the internal `agent-pipe-uploader` Service. It accepts
only signed HTTPS transfer capabilities through configured profiles and has no
AWS, GCP, Azure, or Kubernetes credentials.

The chart mounts a non-secret profile file at
`/etc/agent-pipe/profiles.json` and an isolated artifact PVC at `/artifacts`.
The service exposes Streamable HTTP MCP at `/mcp` and health at `/healthz`.
It does not log signed URLs or artifact bytes.

`test_server.py` is run during the image build and covers artifact-root escape
rejection plus signed-URL host, prefix, and authorization-parameter validation.
