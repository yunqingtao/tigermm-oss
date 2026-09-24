"""
Mobile web server - serves T-Chat UI
Phone: http://10.39.127.156:8080
"""
import http.server
import socketserver
import os

PORT = 8080
DIR = os.path.dirname(os.path.abspath(__file__))

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)
    
    def log_message(self, format, *args):
        print(f"[web] {args[0]}")

with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
    print(f"[web] Serving on http://0.0.0.0:{PORT}")
    print(f"[web] Phone: http://10.39.127.156:{PORT}")
    httpd.serve_forever()
