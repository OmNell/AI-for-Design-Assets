"""按 config.yaml 中的监听地址启动。"""
import uvicorn

from app.config import load_settings


def main() -> None:
    config = load_settings()
    uvicorn.run("app.main:app", host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
