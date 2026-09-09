# gcloud-mcp image

Container image for the Google-owned `@google-cloud/gcloud-mcp` stdio server.
It combines a pinned Google Cloud CLI release with the pinned preview MCP
package; `gcloud-mcp` starts only when `gcloud` is present on `PATH`.

## Runtime contract

This image contains no Google credential, service-account key, static bearer
token, or Terraform state. The consuming ToolHive `MCPServer` must provide:

- a dedicated Kubernetes ServiceAccount and projected OIDC token;
- a non-secret Google Workload Identity Federation credential-configuration
  file mounted from a ConfigMap;
- `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE` pointing to that file;
- writable `HOME`, `CLOUDSDK_CONFIG`, and temporary directories; and
- a restrictive `gcloud-mcp --config` allowlist mounted from a ConfigMap.

The server inherits its environment when it spawns `gcloud`, so the credential
configuration is renewed through Workload Identity Federation rather than
stored in the image. The image must remain cluster-internal behind ToolHive;
its command allowlist and Google IAM role set are separate read boundaries.

## Versioning and delivery

- `GCLOUD_MCP_VERSION` pins the upstream npm package.
- `GOOGLE_CLOUD_CLI_VERSION` pins the Debian `google-cloud-cli` package.
- The Node base image is pinned to an immutable OCI index digest.

Pull-request CI builds this image but does not publish it. A confirmed merge to
`main` publishes `ghcr.io/makeitworkcloud/gcloud-mcp:latest` and a commit-SHA
tag. Consumers must select a verified immutable image reference through their
own GitOps pull request; image publication alone does not deploy it.
