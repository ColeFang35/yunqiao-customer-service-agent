# -*- coding: utf-8 -*-
"""智能客服系统启动入口。

用法:
    python run.py            # 以 .env / 环境变量中的配置启动服务
    python run.py --port 9000
"""
import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="云雀商城智能客服服务")
    parser.add_argument("--host", default=None, help="监听地址")
    parser.add_argument("--port", type=int, default=None, help="监听端口")
    args = parser.parse_args()

    from backend.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "backend.server:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
    )


if __name__ == "__main__":
    main()