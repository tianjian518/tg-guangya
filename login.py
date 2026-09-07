"""光鸭扫码登录一次，把令牌写进 config.yaml。

用法:
    python login.py            # 用默认 config.yaml
    python login.py my.yaml    # 指定配置文件

仅做登录这一件事，不抓取、不转存。适合首次部署或令牌过期时重跑。
"""
from __future__ import annotations

import argparse
import logging

from core.guangya import GuangyaClient, GuangyaError
from core.config import AppConfig

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("login")

DEFAULT_CONFIG = "config.yaml"


def main() -> None:
    ap = argparse.ArgumentParser(description="光鸭云盘扫码登录")
    ap.add_argument("config", nargs="?", default=DEFAULT_CONFIG, help="配置文件路径")
    args = ap.parse_args()

    cfg = AppConfig.load(args.config)
    client = GuangyaClient(
        access_token=cfg.guangya.access_token,
        refresh_token=cfg.guangya.refresh_token,
        client_id=cfg.guangya.client_id,
        device_id=cfg.guangya.device_id,
    )
    try:
        access, refresh = client.login_interactive()
    except (GuangyaError, KeyboardInterrupt) as exc:
        raise SystemExit(f"登录未完成: {exc}")
    cfg.save_token(access, refresh, args.config)
    log.info("令牌已保存至 %s，现在可以运行 python main.py --config %s", args.config, args.config)


if __name__ == "__main__":
    main()
