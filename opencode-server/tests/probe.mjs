// makeitworkcloud-owned synthetic CI fixture, never copied into the image.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const home = "/home/opencode";
const cache = `${home}/.cache/opencode/packages`;
const specs = { "context-mode": "1.0.169", "opencode-mem": "2.26.0" };
const root = (name) => `${cache}/${name}@${specs[name]}/node_modules/${name}`;
const mode = process.argv[2];
const marker = process.argv[3];

if (mode === "security") {
  assert.match(marker, /^ci-cold-[a-f0-9]{32}$/);
  assert.equal(process.geteuid(), 1000);
  assert.equal(process.getegid(), 1000);
  for (const pid of ["self", "1"]) {
    const status = fs.readFileSync(`/proc/${pid}/status`, "utf8");
    const fields = Object.fromEntries(status.split("\n").filter((line) => line.includes(":")).map((line) => {
      const [key, value] = line.split(":");
      return [key, value.trim().split(/\s+/)];
    }));
    assert.equal(fields.Uid[1], "1000");
    assert.equal(fields.Gid[1], "1000");
    assert.equal(fields.NoNewPrivs[0], "1");
    assert.equal(BigInt("0x" + fields.CapEff[0]), 0n);
  }
  const mounts = fs.readFileSync("/proc/self/mountinfo", "utf8").trim().split("\n").map((line) => line.split(" "));
  const rootMounts = mounts.filter((fields) => fields[4] === "/");
  assert.equal(rootMounts.length, 1);
  assert(rootMounts[0][5].split(",").includes("ro"), "effective root mount must be read-only");
  const forbidden = `/usr/local/bin/.${marker}`;
  let denial;
  try {
    fs.writeFileSync(forbidden, marker, { flag: "wx" });
    fs.unlinkSync(forbidden);
  } catch (error) {
    denial = error.code;
  }
  // DAC can deny creation before EROFS; the mount assertion independently proves read-only.
  assert(["EROFS", "EACCES"].includes(denial), "rootfs write must be denied");
  for (const dir of [home, `${home}/.config/opencode`, `${home}/.cache`,
    `${home}/.local/share`, `${home}/.local/share/context-mode`, `${home}/.local/state`, "/tmp"]) {
    const file = `${dir}/.${marker}`;
    fs.writeFileSync(file, marker, { flag: "wx" });
    try {
      assert.equal(fs.readFileSync(file, "utf8"), marker);
    } finally {
      fs.unlinkSync(file);
    }
  }
  console.log("PROBE_OK");
} else if (mode === "empty") {
  assert(!fs.existsSync(cache), "plugin cache must start empty");
  assert(!fs.existsSync(`${home}/.opencode-mem/data/.cache`), "model cache must start empty");
  console.log("PROBE_OK");
} else if (mode === "metadata") {
  const packages = ["bash", "ca-certificates", "gcompat", "git", "libgcc", "libstdc++", "nodejs", "ripgrep"];
  console.log(JSON.stringify({
    alpine: fs.readFileSync("/etc/alpine-release", "utf8").trim(),
    node: process.versions.node,
    apk: execFileSync("apk", ["info", "-v", ...packages], { encoding: "utf8" }).trim().split("\n"),
  }));
} else if (mode === "cache") {
  const plugins = {};
  for (const [name, version] of Object.entries(specs)) {
    const manifest = `${root(name)}/package.json`;
    const pkg = JSON.parse(fs.readFileSync(manifest, "utf8"));
    assert.equal(pkg.name, name);
    assert.equal(pkg.version, version);
    const lock = `${cache}/${name}@${version}/package-lock.json`;
    plugins[name] = {
      version,
      manifestMtime: fs.statSync(manifest).mtimeMs,
      lockSha256: createHash("sha256").update(fs.readFileSync(lock)).digest("hex"),
    };
  }
  const onnx = JSON.parse(fs.readFileSync(`${root("opencode-mem")}/../onnxruntime-node/package.json`, "utf8"));
  assert.equal(onnx.version, "1.20.1");
  const modelRoot = `${home}/.opencode-mem/data/.cache`;
  const digest = createHash("sha256");
  let files = 0;
  let bytes = 0;
  let onnxFiles = 0;
  // Read only public model artifacts in this probe's disposable HOME, not auth/config/log files.
  async function walk(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const file = path.join(dir, entry.name);
      if (entry.isDirectory()) await walk(file);
      else if (entry.isFile()) {
        const stat = fs.statSync(file);
        files++;
        bytes += stat.size;
        if (entry.name.endsWith(".onnx") && stat.size > 0) onnxFiles++;
        digest.update(path.relative(modelRoot, file) + "\0" + stat.size + "\0");
        for await (const chunk of fs.createReadStream(file)) digest.update(chunk);
      } else throw new Error("unexpected model cache entry");
    }
  }
  await walk(modelRoot);
  assert(onnxFiles > 0, "a downloaded ONNX model is required");
  console.log(JSON.stringify({ plugins, onnx: onnx.version, model: { files, bytes, sha256: digest.digest("hex") } }));
} else if (mode === "context-cold" || mode === "context-warm") {
  // Direct Node execution is not OpenCode host tool invocation; only registration is tested there.
  assert.match(marker, /^ci-cold-[a-f0-9]{32}$/);
  const project = `${home}/context-probe`;
  fs.mkdirSync(project, { recursive: true });
  const load = (file) => import(pathToFileURL(`${root("context-mode")}/build/${file}`).href);
  const { loadDatabase } = await load("db-base.js");
  const Database = loadDatabase();
  const db = new Database(`${process.env.CONTEXT_MODE_DIR}/ci-fts.db`);
  if (mode === "context-cold") {
    db.exec("CREATE VIRTUAL TABLE probe USING fts5(content)");
    db.prepare("INSERT INTO probe(content) VALUES (?)").run("probeanchor quartz-payload-9261");
  }
  assert.equal(db.prepare("SELECT content FROM probe WHERE probe MATCH ?").get("probeanchor").content,
    "probeanchor quartz-payload-9261");
  db.close();
  const { ContextModePlugin } = await load("adapters/opencode/plugin.js");
  const plugin = await ContextModePlugin({ directory: project, client: { app: { log: async () => {} } } });
  const ctx = {
    sessionID: marker, messageID: "ci-message", agent: "ci",
    directory: project, worktree: project, abort: new AbortController().signal,
    metadata: () => {},
  };
  const { SessionDB, resolveSessionDbPath } = await load("session/db.js");
  const { OpenCodeAdapter } = await load("adapters/opencode/index.js");
  const sessions = new SessionDB({ dbPath: resolveSessionDbPath({
    projectDir: project, sessionsDir: new OpenCodeAdapter("opencode").getSessionDir(),
  }) });
  try {
    if (mode === "context-cold") {
      assert.equal(sessions.getEvents(ctx.sessionID).length, 0, "cold session must not preexist");
      await plugin["chat.message"]({ sessionID: ctx.sessionID, messageID: ctx.messageID },
        { message: {}, parts: [{ type: "text", text: marker }] });
    }
    // Warm checks the stored event before compaction or execution; it never calls the capture hook.
    const captured = sessions.getEvents(ctx.sessionID).filter((event) => event.type === "user_prompt");
    assert.equal(captured.length, 1);
    assert.equal(captured[0].data, marker, "exact cold-only marker must survive replacement");
  } finally {
    sessions.close();
  }
  for (const name of ["ctx_execute", "ctx_batch_execute", "ctx_index", "ctx_search", "ctx_stats"])
    assert.equal(typeof plugin.tool[name]?.execute, "function");
  const text = (result) => typeof result === "string" ? result : result.output;
  const execution = await plugin.tool.ctx_execute.execute({ language: "shell", code: "printf context-runtime-ok" }, ctx);
  assert(text(execution).includes("context-runtime-ok"), "shell tool must execute");
  if (mode === "context-cold") {
    await plugin.tool.ctx_batch_execute.execute({
      commands: [{ label: "synthetic catalog", command: "printf 'probeanchor quartz-payload-9261\\n'" }],
      queries: ["probeanchor"], concurrency: 1,
    }, ctx);
  }
  const search = await plugin.tool.ctx_search.execute({ queries: ["probeanchor"], limit: 3 }, ctx);
  assert(text(search).includes("quartz-payload-9261"), "FTS tool must recall payload, not just echo query");
  const compact = { context: [] };
  await plugin["experimental.session.compacting"]({ sessionID: ctx.sessionID }, compact);
  assert(compact.context.some((s) => s.includes("session_resume") && s.includes(marker)),
    "compaction must include the cold-only marker");
  console.log("PROBE_OK");
  process.exit(0);
} else {
  throw new Error("unknown probe mode");
}
