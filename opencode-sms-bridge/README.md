# OpenCode SMS bridge

`opencode-sms-bridge` is the private Twilio SMS/MMS ingress and worker for four fixed, existing primary OpenCode agents: `lawnmowerman`, `grillmaster`, `homesteader`, and `homerepair`. It is not a general Twilio API proxy and never accepts an agent, model, tool, session, or routing choice from a caller.

## Runtime modes

One single-replica pod runs two copies of this image:

- `BRIDGE_MODE=ingress` exposes `POST /twilio/inbound` and `GET /healthz`. It validates the complete form-encoded Twilio signature against `CANONICAL_WEBHOOK_URL`, verifies the configured account, destination-number mapping, approved sender, and message SID, then writes one encrypted durable job.
- `BRIDGE_MODE=worker` exposes a loopback-only health endpoint on port `8081`. It claims queued work, downloads Twilio media only from configured HTTPS Twilio hosts, validates content type, magic bytes, size, and audio duration, then calls the fixed OpenCode agent session and sends one bounded reply through Twilio.

The state database stores encrypted message payloads and HMAC sender identifiers. It deliberately marks uncertain outbound sends as `delivery-unknown` rather than retrying and risking duplicate SMS. The first release is intentionally single replica; do not scale it without replacing SQLite queue/session coordination.

## PII-safe operational telemetry

The bridge emits structured lifecycle events to container logs without access logs or payload data. Events may include the fixed agent channel, media count, stage, and a bounded reason; they never include phone numbers, message SID values, message bodies, media URLs, sender hashes, session IDs, credentials, or provider exception detail.

Ingress events distinguish rejected signatures, ignored account/destination/sender combinations, queued messages, and duplicates. Worker events distinguish claimed jobs, unsupported media, OpenCode failures, state-transition skips, uncertain Twilio delivery, and successful sends. The persistent encrypted queue remains authoritative for detailed recovery; do not log or export its contents.

## Required configuration

All required values come from cluster-owned Secret mounts or safe chart values. Do not place values in this repository or chart `values.yaml`.

| Setting | Mode | Purpose |
| --- | --- | --- |
| `ROUTING_CONFIG_PATH` | both | JSON Secret containing the Twilio account ID, approved senders, and exactly four destination-to-primary-agent mappings: one each for `lawnmowerman`, `grillmaster`, `homesteader`, and `homerepair`. |
| `STATE_PATH`, `STATE_ENCRYPTION_KEY`, `SENDER_HASH_KEY` | both | RWO PVC location and independent encryption/HMAC keys. |
| `CANONICAL_WEBHOOK_URL`, `TWILIO_AUTH_TOKEN` | both | Canonical public URL for signature validation and Twilio credential for protected media downloads. |
| `OPENCODE_API_BASE_URL`, `OPENCODE_SERVER_PASSWORD` | worker | Private OpenCode HTTP API endpoint and Basic-auth credential. |
| `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET` | worker | Least-privilege Twilio API Key used only for outbound replies. |
| `WHISPER_URL` | worker, audio MMS | A local Whisper-compatible transcription endpoint. |

Only a signed webhook from a configured approved sender is queued or answered. The bridge invokes the existing primary agent ID, so it receives that agent's normal OpenCode configuration, permissions, MCP availability, and shared instructions. The source allowlist is an ingress identity gate, not standing authorization: existing explicit-confirmation requirements still apply to any mutation requested over SMS.

`OPENCODE_IMAGE_PARTS_ENABLED` defaults to `false`. Set it to `true` only after a configured image-capable OpenCode model and the deployed OpenCode file-part API have been functionally verified. The bridge refuses unsupported image or audio media rather than forwarding unvalidated bytes.

## Optional Messaging Service delivery

`TWILIO_MESSAGING_SERVICE_SID` is an optional worker setting holding a Twilio Messaging Service SID. The SID is a non-secret identifier, not a credential, so it may come from safe chart values while the Twilio API Key credentials stay Secret-mounted. When the setting is non-empty, the worker submits each reply through `messages.create` with exactly `to`, `body`, and `messaging_service_sid`; it never passes a `from_` number on that path. When the setting is empty or unset, the worker keeps the exact existing direct-send behavior and replies `from_` the channel's configured destination number. The choice is fixed per deployment, never per message, and the setting is not added to the worker's required-configuration gate.

Configuring a Messaging Service changes only how replies are submitted to Twilio. A2P 10DLC campaign registration, toll-free verification, and associating the four channel numbers with the Messaging Service are separate manual Twilio-console operations owned by the operator; this bridge neither performs nor validates that association, and an approved-sender end-to-end SMS test remains the delivery gate.

## Ownership and delivery

`makeitworkcloud/images` owns this source image. Its `main` workflow publishes `ghcr.io/makeitworkcloud/opencode-sms-bridge` after merge. `makeitworkcloud/charts` owns the portable Deployment and configuration wiring; `makeitworkcloud/kustomize-cluster` owns the state PVC, Service, `TunnelBinding`, and SOPS-encrypted Secrets. Publication, GitOps selection, reconciliation, health, Twilio webhook configuration, and functional messaging are separate delivery stages.
