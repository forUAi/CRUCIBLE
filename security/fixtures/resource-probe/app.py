import http.server
http.server.HTTPServer(("0.0.0.0", 8000),
                       http.server.BaseHTTPRequestHandler).serve_forever()
