# -*- coding: utf-8 -*-
import sys
import os

# 确保在 Windows 控制台下支持 UTF-8 编码
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 修复 Windows 注册表导致 .js 文件 MIME 类型被误判为 text/plain 的经典问题
import mimetypes
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")

import time
import phoenix as px

def main():
    print("=" * 60)
    print("[INFO] Starting Arize Phoenix Observability Server...")
    print("=" * 60)
    
    # 启动 Phoenix 服务端，监听 6006 端口
    session = px.launch_app(port=6006)
    
    print("\n[OK] Arize Phoenix is running!")
    print(f"[UI] Web UI Dashboard: {session.url}")
    print("[OTLP] OTLP HTTP trace endpoint: http://localhost:6006/v1/traces")
    print("\nKeep this process running to receive telemetry traces...\n")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[STOP] Arize Phoenix stopped.")

if __name__ == "__main__":
    main()
