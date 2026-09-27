# Agent Pipe uploader image

Python/FastMCP image for the existing internal `agent-pipe-uploader` Service.
It accepts signed HTTPS transfer capabilities through configured profiles and
has no AWS, GCP, Azure, or Kubernetes credentials. The chart mounts a non-secret
profile file at `/etc/agent-pipe/profiles.json` and an isolated artifact PVC at
`/artifacts`. Streamable HTTP MCP remains at `/mcp`, with health at `/healthz`.
Signed URLs and response bodies are not returned or logged by the helper.

## Profile contract and rollout

Every profile now requires a non-empty `operations` list containing only
`upload`, `download`, and/or `verify`, without duplicates. Missing, malformed,
unknown, or unlisted operations fail closed; there is no legacy wildcard.
This is intentionally incompatible with profiles that omit `operations`.
The existing `allowedHosts`, `pathPrefixes`, `requiredQueryParameters`, and
integer non-negative `maxBytes` constraints remain mandatory. Each URL must
supply exactly one non-blank value for every required query parameter.
HTTPS exact-host matching, port 443, and no URL userinfo or fragments are
required. Ambiguous traversal, encoded separators, and nested path escapes
are rejected. Redirects are never followed.

Coordinate existing-profile operations lists with the image rollout to avoid
transfer outages. Do not enable a SlideSpeak/vendor profile until **all**
helper pods run this implementation: old pods ignore `operations` and cannot
enforce download-only access. Such a profile should allow `download` and
`verify`, never `upload`. Exact vendor hosts/prefixes and consuming chart
permissions must be reviewed separately; this image ships no vendor profile.

## Transfers and cleanup

- `upload_artifact(profile_name, artifact, signed_put_url)` uses `upload`.
- `download_artifact(profile_name, signed_get_url, destination)` uses `download`
  and returns the relative artifact path, measured bytes, and SHA-256.
- `verify_download(profile_name, signed_get_url)` uses `verify` and now reads
  the **entire** response, returning measured bytes and SHA-256 without keeping
  a file. It costs a full GET, not a one-byte availability probe.
- GETs require HTTP 200, one valid `Content-Length` within `maxBytes`, no
  `Transfer-Encoding`, and no non-identity content encoding. Streaming checks
  the exact HTTP body length; generic zero-byte artifacts remain valid. The
  digest establishes byte identity, not presentation format or authenticity.
- Downloads use a private temporary file in the destination directory and
  atomic hard-link publication that cannot overwrite an existing name. Failed
  transfers remove their temporary file; empty created parent directories can
  remain. The artifact filesystem must support same-directory hard links.
- `inspect_artifact(artifact)` returns measured bytes and SHA-256 for a local
  regular file.
- `remove_artifact(artifact, expected_sha256)` removes exactly one local regular
  file only when its complete SHA-256 matches. It needs no transfer profile,
  never recursively deletes, and refuses root, directories, and symlinks.
  The consuming agent/chart must require explicit approval for this exact
  artifact and digest before calling it; the tool itself is not an approval UI.

All artifact paths must be normalized relative paths, with no symlink
components. Descriptor-relative no-follow access and file identity/change
checks reduce races; the configured artifact mount is trusted. They do not
provide session isolation or defend against a malicious concurrent process
with the same UID, including the final check-to-unlink race. Do not grant
untrusted writers access to the volume.

The owner-approved 90-day retention and fresh-link issuance belong to the
existing storage/workflow, not a background lifecycle in this helper. Local
capacity cleanup is explicit, after retained delivery has been confirmed;
there is no automatic expiry or eviction. Per-transfer limits are not a total
volume quota. Operators must account for concurrent temporary files, full-disk
failures, and interrupted-process leftovers on the bounded artifact volume.
Connections retain a 60-second socket timeout, not a whole-transfer deadline.

## Validation

CI's `transfer-tests` job installs the existing `fastmcp==3.2.4` dependency on
Python 3.13 and runs `python -m unittest -v test_retained_transfers` from this
directory. Tests invoke the original callable functions preserved by FastMCP
3 decorators, use temporary profiles/files and mocked HTTP, and forbid real
socket connections. Image builds depend on this job as well as existing
checks and detection. No new runtime dependency or Containerfile change is
required. PR #30's separate `test_server.py`/build-time test proposal overlaps
this work; its branch and disposition are unchanged.
