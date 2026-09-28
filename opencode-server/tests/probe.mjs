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

if (mode === "empty") {
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
  // Complement the real OpenCode registration check with a direct Node native-plugin probe.
  // This is NOT a substitute for the memory API's embedded-runtime ONNX test.
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
    sessionID: "ci-context-session", messageID: "ci-message", agent: "ci",
    directory: project, worktree: project, abort: new AbortController().signal,
    metadata: () => {},
  };
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
    await plugin["tool.execute.after"]({
      tool: "Read", sessionID: ctx.sessionID, callID: "ci-read", args: { file_path: `${project}/synthetic.ts` },
    }, { title: "Read", output: "export const synthetic = true;", metadata: {} });
  }
  const search = await plugin.tool.ctx_search.execute({ queries: ["probeanchor"], limit: 3 }, ctx);
  assert(text(search).includes("quartz-payload-9261"), "FTS tool must recall payload, not just echo query");
  const compact = { context: [] };
  await plugin["experimental.session.compacting"]({ sessionID: ctx.sessionID }, compact);
  assert(compact.context.some((s) => s.includes("session_resume") && s.includes("synthetic.ts")),
    "synthetic context state must survive replacement");
  console.log("PROBE_OK");
  process.exit(0);
} else {
  throw new Error("unknown probe mode");
}
