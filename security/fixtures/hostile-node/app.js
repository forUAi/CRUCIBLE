// Runtime phase: the pod netns has no route out. Serve so the oracle verifies.
const http = require("http"), net = require("net");
let result = "blocked at runtime: no answer";
const s = new net.Socket();
s.setTimeout(2500);
s.on("error", (e) => { result = "blocked at runtime: " + e.code; });
s.on("timeout", () => { result = "blocked at runtime: timeout"; s.destroy(); });
s.connect(443, "1.1.1.1", () => { result = "REACHED 1.1.1.1:443 at runtime"; s.destroy(); });
setTimeout(() => {
  console.log("RUNTIME_PROBE " + JSON.stringify({ runtime_egress: result }));
  http.createServer((_q, r) => { r.writeHead(200); r.end(result); })
      .listen(8000, "0.0.0.0");
}, 3000);
