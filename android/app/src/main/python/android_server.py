import functools
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer
import serve
_server = None
_lock = threading.Lock()
def start():
    global _server
    with _lock:
        if _server is None:
            handler = functools.partial(serve.Handler, directory=str(Path(__file__).parent))
            _server = ThreadingHTTPServer(('127.0.0.1', 8777), handler)
            threading.Thread(target=_server.serve_forever, daemon=True).start()
        return 'http://127.0.0.1:%d/equidex-mobile.html' % _server.server_port
