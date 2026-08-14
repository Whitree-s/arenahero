import http.server
import json
import socketserver
from functools import partial

from live_params import write_params

PORT = 8742
DIRECTORY = "/Users/mima1234/WorkBuddy/Arena Hero"


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        # 浏览器永不缓存：避免看到旧的/黑的地图页
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        # CORS：允许预览面板(非8742端口)和 file:// 跨源取 stream/ 数据，
        # 否则 fetch 被 Same-Origin Policy 拦截 → 整片黑屏
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        super().end_headers()

    def do_OPTIONS(self):
        """CORS preflight：浏览器发跨域 fetch 前会先发 OPTIONS 探测"""
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        """实时调参入口：monitor 网页滑块 → 写入 stream/live_params.json。

        仅接受 application/json，body 形如 {"new": {...}, "old": {...}}。
        任何越界/非法值被 clamp 或忽略，绝不崩溃。"""
        if self.path.split("?")[0] != "/api/params":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length > 0 else b""
            raw = json.loads(body.decode("utf-8")) if body else {}
            written = write_params(raw)
            payload = json.dumps(written, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except ValueError as e:
            msg = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
        except Exception as e:  # noqa: BLE001
            self.send_response(500)
            self.end_headers()

    def log_message(self, *args):
        pass  # 安静


class QuietTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    with QuietTCPServer(("127.0.0.1", PORT), NoCacheHandler) as httpd:
        print(f"map server on http://127.0.0.1:{PORT} (no-cache)")
        httpd.serve_forever()
