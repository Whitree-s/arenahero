"""临时校验服务器：把 /stream/ 重定向到 stream_local/，用于离线验证 monitor 渲染，
不影响线上 agent 正在写入的 stream/。验证完即可删除。"""
import http.server
import socketserver
import os

PORT = 8743
ROOT = os.path.dirname(os.path.abspath(__file__))
REAL_STREAM = os.path.join(ROOT, "stream_local")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def translate_path(self, path):
        # /stream/... → stream_local/...
        if path.startswith("/stream/"):
            rel = path[len("/stream/"):]
            return os.path.join(REAL_STREAM, rel)
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    with S(("127.0.0.1", PORT), Handler) as httpd:
        print(f"check server on http://127.0.0.1:{PORT} (stream -> stream_local)")
        httpd.serve_forever()
