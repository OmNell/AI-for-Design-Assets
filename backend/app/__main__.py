"""按 config.yaml 中的监听地址启动。"""
import socket

import uvicorn

from app.config import load_settings


def main() -> None:
    config = load_settings()
    if config.nas.dsm_verify_ssl:
        uvicorn.run("app.main:app", host=config.server.host, port=config.server.port)
        return

    from app.main import create_app

    # 使用实际绑定的 IPv4 套接字，避免 Uvicorn 环境变量或启动参数改变监听地址。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", config.server.port))
        application = create_app(config, mac_local_dsm_listener=listener)
        server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=config.server.port,
                                               proxy_headers=False, lifespan="on"))
        server.run(sockets=[listener])


if __name__ == "__main__":
    main()
