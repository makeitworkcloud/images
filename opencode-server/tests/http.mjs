// CI-only loopback transport. stdout is captured by runtime.py, never streamed to logs.
import fs from "node:fs";

function sanitize(value) {
  return String(value).slice(0, 8192)
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/\b(?:https?|ftp|file):\/\/[^\s<>"']+/gi, "[url]")
    .replace(/\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi, "[credential]")
    .replace(/\b(?:password|passwd|token|api[_-]?key|authorization|secret|cookie)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi, "[credential]")
    .replace(/\b(?:sk-|ghp_|github_pat_|hf_)[A-Za-z0-9_-]+/g, "[credential]")
    .replace(/\b[^\s:@/]+:[^\s@/]+@[^\s/]+/g, "[credential]")
    .replace(/\?[^\s<>"']+/g, "[query]")
    .replace(/(?:[A-Za-z]:[\\/]|~\/|\.\.?\/|\/)[^\s<>"'()]+/g, "[path]")
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, " ")
    .slice(0, 768);
}

function diagnostic(value, route, status, source) {
  const chain = [];
  const raw = [];
  function visit(error, depth = 0) {
    if (depth > 5 || chain.length >= 6 || error == null) return;
    if (typeof error === "string") error = { message: error };
    if (typeof error !== "object") return;
    const item = {};
    for (const field of ["name", "message", "code", "status"]) {
      if (typeof error[field] !== "string" && typeof error[field] !== "number") continue;
      raw.push(String(error[field]).slice(0, 8192));
      item[field] = sanitize(error[field]);
    }
    if (Object.keys(item).length) chain.push(item);
    visit(error.error, depth + 1);
    visit(error.cause, depth + 1);
    if (Array.isArray(error.errors)) for (const cause of error.errors.slice(0, 3)) visit(cause, depth + 1);
  }
  visit(value);
  const text = raw.join("\n").toLowerCase();
  const markers = {
    native_loader: ["dlopen", "error loading shared library", "cannot open shared object", "undefined symbol", "symbol not found", "glibc_", "ld-linux", "err_dlopen_failed"],
    missing_package: ["cannot find module", "cannot find package", "err_module_not_found", "module_not_found", "cannot resolve", "could not resolve"],
    network_tls: ["certificate", "unable_to_verify", "self_signed", "cert_has_expired", "unable_to_get_issuer", "ssl"],
    network_dns: ["enotfound", "eai_again", "getaddrinfo"],
    network_connection: ["econnreset", "econnrefused", "etimedout", "und_err", "fetch failed", "socket hang up"],
    model_download_or_remote_http: ["could not locate file", "local_files_only", "remote model", "huggingface", "404", "403"],
    timeout: ["timeout", "timed out", "aborterror"],
  };
  const matches = Object.fromEntries(Object.entries(markers)
    .map(([category, phrases]) => [category, phrases.filter((phrase) => text.includes(phrase))])
    .filter(([, phrases]) => phrases.length));
  return { route, http_status: status, source, error_chain: chain,
    categories: Object.keys(matches).length ? Object.keys(matches) : ["unclassified"], matches };
}

let status = null;
let label = "fixture request";
try {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  const route = input.route.split("?")[0];
  const allowed = input.port === 4096 ? ["/config", "/experimental/tool/ids"]
    : input.port === 4747 ? ["/api/health", "/api/memories", "/api/search"] : [];
  if (!allowed.includes(route)) throw new Error("unsupported fixture route");
  label = `${input.post === null ? "GET" : "POST"} ${route}`;
  const response = await fetch(`http://127.0.0.1:${input.port}${input.route}`, {
    method: input.post === null ? "GET" : "POST",
    headers: input.headers,
    body: input.post === null ? undefined : JSON.stringify(input.post),
    redirect: "error",
    signal: AbortSignal.timeout(input.timeout * 1000),
  });
  status = response.status;
  const chunks = [];
  let bytes = 0;
  let truncated = false;
  for await (const chunk of response.body) {
    bytes += chunk.length;
    if (bytes > 1024 * 1024) {
      truncated = true;
      break;
    }
    chunks.push(chunk);
  }
  let body = null;
  let isJson = false;
  if (!truncated) {
    try {
      body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      isJson = true;
    } catch {}
  }
  const failed = !response.ok || !isJson || body?.success === false;
  const error = failed ? diagnostic(input.port === 4747 && isJson ? body : null,
    label, status, "http_response") : null;
  if (error) {
    error.body_is_json = isJson;
    error.body_truncated = truncated;
  }
  console.log(JSON.stringify({ status, body, diagnostic: error }));
} catch (error) {
  console.log(JSON.stringify({ status, body: null,
    diagnostic: diagnostic(error, label, status, "client_transport") }));
}
