import socket
import threading
import time
import webbrowser

import uvicorn


def open_browser(port):
    for _ in range(60):
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                webbrowser.open(f'http://127.0.0.1:{port}/')
                return
        except OSError:
            time.sleep(0.2)


if __name__ == '__main__':
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    print(f'工作台地址 http://127.0.0.1:{port}/', flush=True)
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()
    server = uvicorn.Server(uvicorn.Config('workbench:app', log_level='warning'))
    server.run(sockets=[listener])
