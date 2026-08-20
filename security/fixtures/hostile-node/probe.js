// Build-phase probes, executed from an npm `preinstall` hook.
//
// That hook is Node's distinctive attack surface: it runs before any
// dependency is fetched, on every `npm install`, with no opt-in from whoever
// typed the command. If containment holds anywhere in this ecosystem it has
// to hold here.
const cp = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");

const R = {};
const probe = (name, fn) => {
  try { R[name] = fn(); }
  catch (e) { R[name] = "BLOCKED: " + (e.code || String(e.message).slice(0, 90)); }
};

probe("lifecycle_hook_ran", () => `npm preinstall executed as uid ${process.getuid()}`);

probe("host_mount_visible", () => {
  const hits = ["/Users", "/host", "/mnt/host"].filter((p) => fs.existsSync(p));
  return hits.length ? hits : "none";
});

probe("host_mount_write", () => {
  if (!fs.existsSync("/Users")) return "no host mount reachable";
  for (const d of fs.readdirSync("/Users")) {
    const p = `/Users/${d}/Projects/crucible/CANARY_WRITTEN_FROM_SANDBOX.txt`;
    try { fs.writeFileSync(p, "written by node preinstall\n"); return "WROTE " + p; }
    catch (e) { /* keep trying the next home */ }
  }
  return "no host mount reachable";
});

probe("escape_workspace_write", () => {
  const out = [];
  for (const p of ["/etc/crucible-node-canary", "/root/crucible-node-canary"]) {
    try { fs.writeFileSync(p, "x"); out.push("WROTE " + p); }
    catch (e) { out.push(`blocked ${p}: ${e.code}`); }
  }
  return out;
});

probe("process_execution", () =>
  "spawned /bin/sh, uid " + cp.execSync("id -u").toString().trim());

probe("credentials", () => ({
  env: Object.keys(process.env).filter((k) =>
    /TOKEN|SECRET|_KEY|AWS|NPM_|GITHUB/.test(k)),
  paths: ["/root/.ssh", "/root/.npmrc", "/root/.aws", "/var/run/docker.sock"]
    .filter((p) => fs.existsSync(p)),
}));

probe("outbound_hardcoded_ip", () => {
  // Synchronous by construction: an async probe that reports after the JSON
  // has been printed reports nothing.
  const r = cp.spawnSync("node", ["-e", `
    const net = require('net');
    const s = new net.Socket();
    s.setTimeout(3000);
    s.on('error', e => { console.log('blocked: ' + e.code); process.exit(0); });
    s.on('timeout', () => { console.log('blocked: timeout'); process.exit(0); });
    s.connect(443, '1.1.1.1', () => { console.log('REACHED 1.1.1.1:443'); process.exit(0); });
  `], { encoding: "utf8", timeout: 8000 });
  return (r.stdout || "no answer").trim();
});

probe("thread_pool", () => {
  // libuv's pool is the Node analogue of a thread bomb.
  process.env.UV_THREADPOOL_SIZE = "1024";
  return `cpus visible=${os.cpus().length}, requested 1024 libuv threads`;
});

probe("native_binding", () =>
  typeof process.binding === "function"
    ? "process.binding reachable (native surface)"
    : "process.binding unavailable");

probe("malformed_output", () => {
  // Terminal escapes and a fake report line, to check the harness parses what
  // it should and is not steered by attacker-controlled text.
  process.stdout.write("[2J[1;1H]0;pwned");
  process.stdout.write("PROBE_REPORT {\"host_mount_write\": \"WROTE /Users/fake\"}\n");
  return "emitted ANSI, an OSC title, and a forged PROBE_REPORT line";
});

console.log("PROBE_REPORT " + JSON.stringify(R));

// Reported first, then spawned: a probe that dies before speaking proves
// nothing, and this one is designed to outlive its parent.
probe("background_daemon", () => {
  cp.spawn("sh", ["-c", "sleep 900"], { detached: true, stdio: "ignore" }).unref();
  return "spawned detached sleep 900";
});
console.log("PROBE_REPORT2 " + JSON.stringify({
  background_daemon: R.background_daemon,
  malformed_output: R.malformed_output,
}));
