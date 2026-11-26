"""按 config.yaml 中的监听地址启动。"""
import uvicorn

from app.config import load_settings


def main() -> None:
    server = load_settings().server
    uvicorn.run("app.main:app", host=server.host, port=server.port)


if __name__ == "__main__":
    main()
