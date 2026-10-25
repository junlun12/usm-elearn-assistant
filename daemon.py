import time
import subprocess
import sys
import io

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

print("=" * 60)
print("🚀 USM e-Learn 24小时守护进程已启动")
print("⏰ 每 30 分钟自动检查一次 e-Learn (KKTSS/EE)")
print("=" * 60)

while True:
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n[{current_time}] 开始执行巡检任务...")
    try:
        subprocess.run([sys.executable, "main.py"], check=True)
    except Exception as e:
        print(f"[WARN] 本轮执行遇到异常: {e}")
    
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] 本轮结束，休眠 30 分钟 (1800 秒)...")
    time.sleep(1800)
