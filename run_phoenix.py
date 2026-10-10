# -*- coding: utf-8 -*-
"""
启动 Arize Phoenix 可观测性服务端（正式 serve 入口，支持数据持久化）
1. 配置来源: .env (PHOENIX_PORT / PHOENIX_WORKING_DIR / PHOENIX_SQL_DATABASE_URL)
2. 持久化: Trace 数据写入 PHOENIX_SQL_DATABASE_URL 指定的 SQLite 文件，重启后历史数据不丢失
3. 注意: 不使用 px.launch_app() —— 它面向 notebook 临时会话，会强制使用临时目录数据库并忽略持久化配置
"""
import sys
import os

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 修复 Windows 注册表导致 .js 文件 MIME 类型被误判为 text/plain 的经典问题
import mimetypes
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")

# 在初始化 Phoenix 之前加载 .env，确保端口与持久化配置在服务端读取配置前已经生效
from dotenv import load_dotenv
load_dotenv()


def main():
    print("=" * 60)
    print("[INFO] Starting Arize Phoenix Observability Server...")
    print("=" * 60)

    # 持久化配置检查 (配置位于 .env: PHOENIX_WORKING_DIR / PHOENIX_SQL_DATABASE_URL)
    working_dir = os.getenv("PHOENIX_WORKING_DIR")
    db_url = os.getenv("PHOENIX_SQL_DATABASE_URL")
    port = os.getenv("PHOENIX_PORT", "6006")
    if working_dir:
        os.makedirs(working_dir, exist_ok=True)
    if db_url:
        print(f"[DB] Trace 持久化已启用: {db_url}")
        print("[DB] 重启 run_phoenix.py 后，历史 Trace 仍可在 Web UI 中查看")
    else:
        print("[WARN] 未配置 PHOENIX_SQL_DATABASE_URL，数据仅存于临时目录，重启后会丢失")

    print(f"\n[UI] Web UI Dashboard: http://localhost:{port}")
    print(f"[OTLP] OTLP HTTP trace endpoint: http://localhost:{port}/v1/traces")
    print("\nKeep this process running to receive telemetry traces...")

    # 正式服务入口: python -m phoenix.server.main serve (读取上述环境变量)
    from phoenix.server.main import main as serve_main
    sys.argv = [sys.argv[0], "serve"]
    serve_main()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[STOP] Arize Phoenix stopped.")
