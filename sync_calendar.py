import os
import sys
import io
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

EVENTS_FILE = os.path.join(os.path.dirname(__file__), "data", "events.json")
SA_FILE = os.path.join(os.path.dirname(__file__), "data", "service_account.json")
CALENDAR_ID = "a6975e5c06f324204d4b1c2aac4e2af362e89ea7d505f2a837483584345ccb06@group.calendar.google.com"

def main():
    if not os.path.exists(EVENTS_FILE):
        print(f"[ERROR] 未找到待办事件文件 {EVENTS_FILE}")
        return
    if not os.path.exists(SA_FILE):
        print(f"[ERROR] 未找到 Google 凭据文件 {SA_FILE}")
        return

    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
        events = json.load(f)

    print(f"[INFO] 正在连接 Google Calendar API 并准备同步 {len(events)} 个日程...")
    try:
        credentials = service_account.Credentials.from_service_account_file(
            SA_FILE,
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        service = build("calendar", "v3", credentials=credentials)
    except Exception as e:
        print(f"[ERROR] 初始化 Google Calendar 凭据失败: {e}")
        return

    success_count = 0
    for idx, ev in enumerate(events, 1):
        summary = ev.get("summary", "USM e-Learn 待办")
        desc = ev.get("description", "")
        start_iso = ev.get("start_iso", "")
        end_iso = ev.get("end_iso") or start_iso

        if not start_iso:
            continue

        if "T" in start_iso:
            start_obj = {"dateTime": start_iso, "timeZone": "Asia/Kuala_Lumpur"}
            end_obj = {"dateTime": end_iso, "timeZone": "Asia/Kuala_Lumpur"}
        else:
            start_obj = {"date": start_iso[:10]}
            end_obj = {"date": end_iso[:10]}

        event_body = {
            "summary": summary,
            "description": desc,
            "start": start_obj,
            "end": end_obj,
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 24 * 60},  # 提前 1 天
                    {"method": "popup", "minutes": 120}       # 提前 2 小时
                ]
            }
        }
        try:
            service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
            print(f"[{idx}/{len(events)}] ✅ 已写入 Google 日历: {summary} ({start_iso[:16].replace('T', ' ')})")
            success_count += 1
        except Exception as e:
            print(f"[{idx}/{len(events)}] ❌ 写入失败 [{summary}]: {e}")

    print(f"\n🎉 同步完成！成功将 {success_count}/{len(events)} 个日程写入 Google 日历！")

if __name__ == "__main__":
    main()
