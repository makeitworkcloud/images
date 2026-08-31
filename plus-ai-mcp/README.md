# Plus AI MCP adapter

This image exposes a restricted, stateless Streamable HTTP MCP server for the Plus AI Presentation APIs. It keeps the Plus API key inside the cluster and does not use the provider's OAuth-only hosted MCP endpoint.

## Tools

- `create_presentation` and `get_presentation` wrap the template-based Presentations API; completed jobs return a PPTX URL.
- `create_presentation_with_agent` and `get_presentation_agent_session` wrap the asynchronous Presentation Agent API; completed sessions return PPTX, PDF, and thumbnail URLs.

The adapter intentionally does not implement Plus file upload, arbitrary callbacks, or arbitrary outbound URLs. The deployment injects `PLUSAI_API_KEY` from an encrypted Kubernetes Secret through the ToolHive `MCPServer` resource.

The backend is intended only for the existing ToolHive ClusterIP gateway. Its Streamable HTTP transport is stateless and disables backend Host-header DNS-rebinding checks because ToolHive's internal proxy Service does not provide a stable Host header; the gateway's existing cluster-internal trust boundary remains responsible for client access.
