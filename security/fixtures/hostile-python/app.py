"""Runtime phase: the netns has no route out. Serve so the oracle can verify."""
import http.server, json, socket, threading

def probe():
    try:
        s = socket.socket(); s.settimeout(3); s.connect(("1.1.1.1", 443))
        return "REACHED 1.1.1.1:443 at runtime"
    except OSError as e:
        return f"blocked at runtime: errno {e.errno}"

RESULT = {"runtime_egress": probe()}
print("RUNTIME_PROBE " + json.dumps(RESULT), flush=True)

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps(RESULT).encode())
    def log_message(self, *a): pass

http.server.HTTPServer(("0.0.0.0", 8000), H).serve_forever()
