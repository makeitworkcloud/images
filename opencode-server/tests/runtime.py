#!/usr/bin/env python3
"""Synthetic CI-only adaptation of charts PR 113 at a34d8ef1646470c5d24275660c72d314f63b617b.

Retains its real memory API write/search/id/content/similarity contract. Uses
Podman's local Buildah store, never pulls a replacement image. No host ports,
credentials, provider calls, live services, or production data. See ../README.md.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import quote
import uuid

HOME = "/home/opencode"
CONFIG = HOME + "/.config/opencode"
SENTENCE = "The synthetic runtime probe stores amber-harbor-7f31d2 evidence."
TAG = "opencode_project_" + hashlib.sha256(("path:" + HOME).encode()).hexdigest()[:16]
TOKEN = "ci-fixture-not-a-secret"  # Public fixture, not a credential.
PLUGINS = ["context-mode@1.0.169", "opencode-mem@2.26.0"]
TOOLS = {"memory", "ctx_execute", "ctx_batch_execute", "ctx_index", "ctx_search", "ctx_stats"}
RUN_ID = uuid.uuid4().hex
PREFIX = "opencode-runtime-ci-" + RUN_ID
COLD_MARKER = "ci-cold-" + RUN_ID
VOLUME = PREFIX + "-home"
CONTAINERS = [PREFIX + "-cold", PREFIX + "-warm"]
CONFIG_VOLUMES = [PREFIX + "-config-cold", PREFIX + "-config-warm"]
PREP = PREFIX + "-prepare"
DEADLINE = time.monotonic() + 25 * 60
METRICS = {}
STAGE = "preflight"
DETAILS = []
REQUEST_ERROR = None
CREATED_VOLUMES = []
TESTS = str(Path(__file__).resolve().parent)
ENV = [
    "-e", "HOME=" + HOME,
    "-e", "XDG_CONFIG_HOME=" + HOME + "/.config",
    "-e", "XDG_CACHE_HOME=" + HOME + "/.cache",
    "-e", "XDG_DATA_HOME=" + HOME + "/.local/share",
    "-e", "XDG_STATE_HOME=" + HOME + "/.local/state",
    "-e", "CONTEXT_MODE_DIR=" + HOME + "/.local/share/context-mode",
]
HARDEN = [
    "--read-only", "--read-only-tmpfs=false", "--cap-drop=ALL",
    "--security-opt=no-new-privileges", "--tmpfs=/tmp:rw,size=512m,mode=1777",
]
OPENCODE = {"plugin": PLUGINS, "enabled_providers": [], "permission": {"*": "deny"}, "mcp": {}}
MEMORY = {
    "storagePath": HOME + "/.opencode-mem/data",
    "embeddingModel": "Xenova/nomic-embed-text-v1", "embeddingDimensions": 768,
    "embeddingUseTaskPrefixes": True,
    "autoCaptureEnabled": False, "autoCleanupEnabled": False, "injectProfile": False,
    "userProfileAutoCleanupEnabled": False, "userProfileValidationEnabled": False,
    "chatMessage": {"enabled": False}, "compaction": {"enabled": False},
    "containerTagPrefix": "opencode", "webServerEnabled": True,
    "webServerHost": "127.0.0.1", "webServerPort": 4747, "webServerApiToken": TOKEN,
    "similarityThreshold": 0.6,
}


class Failure(Exception):
    def __init__(self, message, *, operation=None, diagnostic=None):
        super().__init__(message)
        self.operation = operation
        self.diagnostic = diagnostic


def run(args, timeout=60, check=True, cleanup=False, input_text=None):
    operation = "podman " + " ".join(args[:2] if args[0] in ("image", "volume", "container") else args[:1])
    remaining = timeout if cleanup else min(timeout, DEADLINE - time.monotonic())
    if remaining <= 0:
        raise Failure("deadline exhausted", operation=operation)
    try:
        result = subprocess.run(["podman", *args], input=input_text, capture_output=True,
                                text=True, timeout=remaining)
    except subprocess.TimeoutExpired:
        raise Failure("subprocess timeout", operation=operation) from None
    except OSError:
        raise Failure("could not start container command", operation=operation) from None
    if result.returncode and check:
        DETAILS.append(result.stderr)
        diagnostic = {"returncode": result.returncode}
        if args[:2] == ["image", "inspect"]:
            # Emit only fixed vocabulary, never stderr lines, argv, paths or inspect JSON.
            allowlist = {
                "template": ("template:", "can't evaluate field", 'function "json" not defined', "executing template"),
                "image_reference": ("no such image", "image not known", "image not found", "invalid reference format", "short-name"),
                "storage": ("storage", "database", "graphroot", "runroot", "overlay", "mount program", "db configuration mismatch"),
                "permission": ("permission denied", "operation not permitted"),
                "runtime": ("cannot clone", "cannot re-exec", "user namespace", "newuidmap", "newgidmap"),
            }
            sample = result.stderr[:8192].lower()
            matches = {category: [phrase for phrase in phrases if phrase in sample]
                       for category, phrases in allowlist.items()}
            diagnostic.update({
                "stderr_matches": {category: phrases for category, phrases in matches.items() if phrases},
                "stderr_present": bool(result.stderr),
                "stderr_scan_truncated": len(result.stderr) > 8192,
            })
        raise Failure("container command failed (rc=%d)" % result.returncode,
                      operation=operation, diagnostic=diagnostic)
    return result


def phase(name):
    global STAGE
    STAGE = name
    print("[phase] " + name, flush=True)
    return time.monotonic()


def elapsed(name, start):
    METRICS[name] = round(time.monotonic() - start, 3)
    print("[seconds] %s=%s" % (name, METRICS[name]), flush=True)


def request(container, port, route, post=None, timeout=30):
    global REQUEST_ERROR
    REQUEST_ERROR = {"route": ("POST " if post is not None else "GET ") + route.split("?")[0],
                     "http_status": None, "source": "client_process"}
    headers = {"x-opencode-directory": HOME}
    if port == 4747:
        headers["Authorization"] = "Bearer " + TOKEN
    if post is not None:
        headers["Content-Type"] = "application/json"
    result = run(["exec", "-i", container, "node", "/probe/http.mjs"], timeout=timeout + 10,
                 input_text=json.dumps({"port": port, "route": route, "post": post,
                                        "headers": headers, "timeout": timeout}))
    try:
        response = json.loads(result.stdout)
        status = response["status"]
        payload = response["body"]
        REQUEST_ERROR = response["diagnostic"]
    except (ValueError, KeyError, TypeError):
        raise Failure("request helper returned invalid envelope", operation="loopback HTTP request") from None
    if REQUEST_ERROR is not None:
        DETAILS.append(json.dumps(REQUEST_ERROR))
    if not isinstance(status, int) or not 200 <= status < 300:
        return None
    return payload


def wait_json(container, port, route, accept, window=300):
    end = min(DEADLINE, time.monotonic() + window)
    while time.monotonic() < end:
        data = request(container, port, route, timeout=max(1, min(30, int(end - time.monotonic()))))
        if data is not None and accept(data):
            return data
        state = run(["inspect", "--format", "{{.State.Status}}", container]).stdout.strip()
        if state != "running":
            raise Failure("container stopped before readiness")
        time.sleep(2)
    raise Failure("readiness deadline exhausted")


def probe(container, mode, timeout=120):
    result = run(["exec", container, "node", "/probe/probe.mjs", mode, COLD_MARKER], timeout=timeout)
    if mode in ("cache", "metadata"):
        return json.loads(result.stdout)
    if "PROBE_OK" not in result.stdout.splitlines():
        raise Failure("probe completion marker absent")


def config_present(data):
    if not isinstance(data, dict):
        return False
    specs = [p[0] if isinstance(p, list) and p else p for p in data.get("plugin", [])]
    return all(p in specs for p in PLUGINS) and data.get("enabled_providers") == [] and not data.get("mcp")


def prepare(image, index):
    config_volume = CONFIG_VOLUMES[index]
    CREATED_VOLUMES.append(config_volume)
    run(["volume", "create", config_volume])
    mounts = ["-v", VOLUME + ":" + HOME, "-v", config_volume + ":" + CONFIG]
    # CHOWN cannot bypass uid-1000 directory permissions. Only visit fresh
    # copy-up trees; never traverse private caches during warm replacement.
    run(["run", "--rm", "--name", PREP, "--pull=never", "--network=none",
         *HARDEN, "--user=0:0", "--cap-add=CHOWN", *mounts,
         "--entrypoint=/bin/sh", image, "-ec",
         "chown -R 1000:1000 " + (HOME if index == 0 else CONFIG)], timeout=120)
    seed = (
        "const fs=require('node:fs');const data=JSON.parse(fs.readFileSync(0,'utf8'));"
        "for(const dir of ['.cache','.local/share/context-mode','.local/state'])"
        "fs.mkdirSync('/home/opencode/'+dir,{recursive:true});"
        "for(const [name,value] of Object.entries(data))"
        "fs.writeFileSync('/home/opencode/.config/opencode/'+name,JSON.stringify(value));"
    )
    run(["run", "--rm", "-i", "--name", PREP, "--pull=never", "--network=none",
         *HARDEN, "--user=1000:1000", *ENV, *mounts, "--entrypoint=node", image, "-e", seed],
        input_text=json.dumps({"opencode.json": OPENCODE, "opencode-mem.jsonc": MEMORY}))
    return mounts


def start(image, index, mounts):
    name = CONTAINERS[index]
    # Preserve the inherited entrypoint; arguments match the chart's web command.
    run(["run", "-d", "--name", name, "--pull=never", "--platform=linux/amd64",
         "--network=" + ("bridge" if index == 0 else "none"), *HARDEN,
         "--user=1000:1000", *ENV, *mounts, "-v", TESTS + ":/probe:ro",
         "-w", HOME, image, "web", "--hostname", "127.0.0.1", "--port", "4096"])
    probe(name, "security")
    wait_json(name, 4096, "/config", config_present, window=420)
    return name


def ready(name):
    ids = wait_json(name, 4096, "/experimental/tool/ids",
                    lambda data: isinstance(data, list) and TOOLS.issubset(set(data)))
    if any(ids.count(tool) != 1 for tool in TOOLS):
        raise Failure("duplicate native tool registration")
    wait_json(name, 4747, "/api/health",
              lambda data: isinstance(data, dict) and data.get("success") is True and data.get("status") == "ok")


def recall(name, memory_id):
    payload = request(name, 4747, "/api/search?q=" + quote(SENTENCE) + "&tag=" + TAG + "&pageSize=20", timeout=180)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise Failure("memory search failed")
    hits = [item for item in (payload.get("data") or {}).get("items", [])
            if item.get("type") == "memory" and item.get("id") == memory_id]
    if not hits or hits[0].get("content") != SENTENCE:
        raise Failure("memory id/content not recalled")
    similarity = hits[0].get("similarity")
    if (isinstance(similarity, bool) or not isinstance(similarity, (float, int))
            or not math.isfinite(similarity) or similarity < 0.6):
        raise Failure("memory similarity must be finite and at least 0.6")
    return similarity


def main():
    status = "FAIL"
    exit_code = 1
    evidence = {"context_host_invocation": "outstanding; registration only"}
    try:
        if len(sys.argv) != 2:
            raise Failure("exact locally built image argument required")
        image = sys.argv[1]
        phase("preflight: podman image inspect")
        inspected = run(["image", "inspect", image])
        try:
            records = json.loads(inspected.stdout)
            if not isinstance(records, list) or len(records) != 1:
                raise ValueError
            record = records[0]
            identity = {"id": record["Id"], "arch": record["Architecture"],
                        "os": record["Os"], "entrypoint": record["Config"]["Entrypoint"]}
        except (ValueError, KeyError, TypeError):
            raise Failure("image inspect returned unexpected JSON structure",
                          operation="podman image inspect") from None
        if not isinstance(identity["id"], str) or not identity["id"]:
            raise Failure("image inspect returned no image ID", operation="podman image inspect")
        if identity["arch"] != "amd64" or identity["os"] != "linux" or identity["entrypoint"] != ["opencode"]:
            raise Failure("image platform or inherited entrypoint mismatch")
        evidence["image"] = identity
        del records, record, inspected
        # All subsequent containers use this immutable local image ID, not a mutable tag.
        image = identity["id"]
        phase("preflight: create HOME volume")
        CREATED_VOLUMES.append(VOLUME)
        run(["volume", "create", VOLUME])
        phase("preflight: prepare cold volumes")
        mounts = prepare(image, 0)
        helper = ["run", "--rm", "--name", PREP, "--pull=never", "--network=none",
                  *HARDEN, "--user=1000:1000", *ENV, *mounts, "-v", TESTS + ":/probe:ro",
                  "--entrypoint=node", image, "/probe/probe.mjs"]
        phase("preflight: verify empty caches")
        run([*helper, "empty"])
        phase("preflight: runtime metadata")
        evidence["runtime"] = json.loads(run([*helper, "metadata"]).stdout)
        cold = phase("cold startup and automatic pinned plugin installation")
        name = start(image, 0, mounts)
        evidence["cold_security_checked"] = True
        elapsed("cold_config_seconds", cold)
        ready(name)
        elapsed("cold_plugin_ready_seconds", cold)
        # Plugin startup begins background warmup. Write latency includes any remaining warmup.
        first = phase("first local embedding write")
        payload = request(name, 4747, "/api/memories", {"content": SENTENCE, "containerTag": TAG}, timeout=300)
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise Failure("first memory write failed; no compatibility fallback")
        memory_id = (payload.get("data") or {}).get("id")
        if not isinstance(memory_id, str) or not memory_id:
            raise Failure("memory write returned no id")
        elapsed("first_embedding_write_seconds", first)
        elapsed("cold_to_first_write_seconds", cold)
        first = phase("first local embedding recall")
        evidence["cold_similarity"] = recall(name, memory_id)
        elapsed("first_recall_seconds", first)
        phase("direct Node context FTS5, execution, and cold-only synthetic capture")
        probe(name, "context-cold")
        before = probe(name, "cache", timeout=180)
        evidence["cold_cache"] = before
        phase("stop and replace, retain HOME but discard config volume and tmp")
        run(["stop", "--time=30", name], timeout=60)
        run(["rm", name])
        run(["volume", "rm", CONFIG_VOLUMES[0]])
        CREATED_VOLUMES.remove(CONFIG_VOLUMES[0])
        mounts = prepare(image, 1)
        warm = phase("warm replacement with no network")
        name = start(image, 1, mounts)
        evidence["warm_security_checked"] = True
        elapsed("warm_config_seconds", warm)
        ready(name)
        elapsed("warm_plugin_ready_seconds", warm)
        first = phase("warm local embedding recall of same id/content")
        evidence["warm_similarity"] = recall(name, memory_id)
        elapsed("warm_recall_seconds", first)
        elapsed("warm_to_recall_seconds", warm)
        phase("direct Node context recall of exact cold-only event marker")
        probe(name, "context-warm")
        evidence["context_cold_marker_recalled"] = COLD_MARKER
        after = probe(name, "cache", timeout=180)
        if before != after:
            raise Failure("plugin lock/manifest or model cache changed during offline replacement")
        evidence["offline_cache_reused"] = True
        status, exit_code = "PASS", 0
    except (Failure, ValueError, KeyError, OSError, TypeError, AttributeError) as error:
        # Only Failure messages are our fixed strings, never arbitrary exception text.
        evidence["failed_phase"] = STAGE
        evidence["failure_type"] = type(error).__name__
        if REQUEST_ERROR is not None:
            evidence["request_error"] = REQUEST_ERROR
        if isinstance(error, Failure):
            evidence["reason"] = str(error)
            if error.operation is not None:
                evidence["failed_operation"] = error.operation
            if error.diagnostic is not None:
                evidence["command_diagnostic"] = error.diagnostic
        for name in CONTAINERS:
            try:
                logs = run(["logs", "--tail=100", name], check=False, cleanup=True, timeout=15)
                DETAILS.extend([logs.stdout, logs.stderr])
            except Failure:
                pass
        detail = "\n".join(DETAILS).lower()
        evidence["native_load_suspected"] = any(marker in detail for marker in (
            "dlopen", "error loading shared library", "cannot open shared object",
            "undefined symbol", "cannot locate symbol", "ld-linux-x86-64.so.2"))
        evidence["install_or_transport_suspected"] = any(marker in detail for marker in (
            "eacces", "enotfound", "fetch failed", "eresolve", "timed out", "connection refused"))
    finally:
        # A timed-out client can leave its helper behind, so helpers are named too.
        cleanup_ok = True
        for name in [*CONTAINERS, PREP]:
            try:
                exists = run(["container", "exists", name], check=False, cleanup=True, timeout=15)
                if exists.returncode == 0:
                    run(["rm", "-f", name], cleanup=True, timeout=30)
                elif exists.returncode != 1:
                    cleanup_ok = False
            except Failure:
                cleanup_ok = False
        for volume in reversed(CREATED_VOLUMES):
            try:
                exists = run(["volume", "exists", volume], check=False, cleanup=True, timeout=15)
                if exists.returncode == 0:
                    run(["volume", "rm", volume], cleanup=True, timeout=30)
                elif exists.returncode != 1:
                    cleanup_ok = False
            except Failure:
                cleanup_ok = False
        if not cleanup_ok:
            status, exit_code = "FAIL", 1
        report = {"status": status, "cleanup_ok": cleanup_ok, "seconds": METRICS, "evidence": evidence}
        output = json.dumps(report, indent=2)
        print(output, flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf8") as summary:
                summary.write("## OpenCode Alpine runtime prototype\n\n```json\n" + output + "\n```\n")
    return exit_code


def interrupted(_signum, _frame):
    raise Failure("interrupted")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    sys.exit(main())
