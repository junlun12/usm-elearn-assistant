import os
import sys
import io
import re
import json
import hashlib
import html
from datetime import datetime
import requests
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types
from google.oauth2 import service_account
from googleapiclient.discovery import build
import getpass

# 确保 Windows 终端环境下正常输出 UTF-8 中文与 Emoji，并开启即时行缓冲
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

# ================= 自动加载 .env 文件 =================
ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(ENV_FILE):
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

# ================= 环境变量配置 =================
USM_USERNAME = os.environ.get("USM_USERNAME", "teejunlun@student.usm.my")
USM_PASSWORD = os.environ.get("USM_PASSWORD", "")

if not USM_PASSWORD:
    USM_PASSWORD = getpass.getpass("请输入你的 USM 登录密码 (输入时不显示字符): ").strip()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL_NAME = "gemini-3.5-flash-lite" # 每天500次高额度模型

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

STATE_FILE = "data/state.json"

# ================= 校区与专业过滤规则 =================
INCLUDE_CAMPUS = [
    "kktss", "engineering campus", "kampus kejuruteraan", 
    "ee engineering", "electrical and electronics", "electric & electronic"
]
EXCLUDE_CAMPUS = [
    "main campus", "kampus induk", "[for ep]", "for ep", 
    "kampus kesihatan", "health campus"
]
PLAN_KEYWORDS = ["plan", "teaching", "assessment", "pelan", "tugasan", "schedule", "rubrik", "outline", "delivery"]

# ================= 辅助函数 =================
def is_relevant_for_user(text_to_check):
    t = text_to_check.lower()
    if any(ex in t for ex in EXCLUDE_CAMPUS) and not any(inc in t for inc in INCLUDE_CAMPUS):
        return False
    return True

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"seen_ids": [], "initialized": False}
    return {"seen_ids": [], "initialized": False}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram 配置未设置，跳过发送")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # Telegram 限制单条消息最大 4096 字符，安全切片阈值为 3500 字符
    MAX_LEN = 3500
    if len(message) <= MAX_LEN:
        chunks = [message]
    else:
        chunks = []
        current_chunk = []
        current_len = 0
        for line in message.split("\n"):
            if current_len + len(line) + 1 > MAX_LEN:
                if current_chunk:
                    chunks.append("\n".join(current_chunk))
                current_chunk = [line]
                current_len = len(line) + 1
            else:
                current_chunk.append(line)
                current_len += len(line) + 1
        if current_chunk:
            chunks.append("\n".join(current_chunk))

    for idx, chunk in enumerate(chunks, 1):
        chunk_to_send = chunk
        if len(chunks) > 1:
            chunk_to_send = f"<i>(第 {idx}/{len(chunks)} 部分)</i>\n" + chunk

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk_to_send,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            res = requests.post(url, json=payload, timeout=15)
            print(f"[INFO] Telegram 发送响应 ({idx}/{len(chunks)}): {res.status_code}")
            if res.status_code != 200:
                print(f"[WARN] Telegram 错误详情: {res.text}")
                # 如果是 HTML 解析错误，剥离 HTML 标签后纯文本重发兜底
                clean_text = re.sub(r'<[^>]+>', '', chunk_to_send)
                plain_payload = {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": clean_text,
                    "disable_web_page_preview": True
                }
                fb_res = requests.post(url, json=plain_payload, timeout=15)
                print(f"[INFO] Telegram 纯文本降级重发响应: {fb_res.status_code}")
        except Exception as e:
            print(f"[WARN] Telegram 发送失败: {e}")

def sync_to_google_calendar(events):
    sa_path = os.path.join(os.path.dirname(__file__), "data", "service_account.json")
    cal_id = os.environ.get("GOOGLE_CALENDAR_ID", "a6975e5c06f324204d4b1c2aac4e2af362e89ea7d505f2a837483584345ccb06@group.calendar.google.com")
    if not os.path.exists(sa_path):
        print("[WARN] 未找到 data/service_account.json，跳过日历写入")
        return
    try:
        credentials = service_account.Credentials.from_service_account_file(
            sa_path,
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        service = build("calendar", "v3", credentials=credentials)
        print(f"[INFO] 正在向 Google 日历同步 {len(events)} 个日程...")
        success_cnt = 0
        for ev in events:
            s_iso = ev.get("start_iso", "")
            e_iso = ev.get("end_iso") or s_iso
            if not s_iso:
                continue
            if "T" in s_iso:
                start_obj = {"dateTime": s_iso, "timeZone": "Asia/Kuala_Lumpur"}
                end_obj = {"dateTime": e_iso, "timeZone": "Asia/Kuala_Lumpur"}
            else:
                start_obj = {"date": s_iso[:10]}
                end_obj = {"date": e_iso[:10]}
            event_body = {
                "summary": ev.get("summary"),
                "description": ev.get("description", ""),
                "start": start_obj,
                "end": end_obj,
                "reminders": {
                    "useDefault": False,
                    "overrides": [
                        {"method": "popup", "minutes": 24 * 60},
                        {"method": "popup", "minutes": 120}
                    ]
                }
            }
            try:
                service.events().insert(calendarId=cal_id, body=event_body).execute()
                print(f"  [SUCCESS] Google 日历已写入: {ev.get('summary')}")
                success_cnt += 1
            except Exception as item_err:
                print(f"  [WARN] 写入日历条目异常 [{ev.get('summary')}]: {item_err}")
        print(f"[INFO] Google 日历同步完成，成功写入 {success_cnt}/{len(events)} 条！")
    except Exception as e:
        print(f"[ERROR] 连接 Google 日历服务异常: {e}")

def filter_future_events(events):
    valid = []
    now = datetime.now()
    for ev in events:
        s = ev.get("start_iso", "")
        try:
            # 过滤掉 2030 年等离谱年份错误，锁定 2026/2027 学年
            ev_dt = datetime.fromisoformat(s.replace("Z", ""))
            if ev_dt.year > 2027:
                continue
            if ev_dt >= now:
                valid.append(ev)
        except Exception:
            valid.append(ev)
    return valid

# ================= 1. Gemini AI 深度提取与消重 =================
def analyze_content_with_ai(content_data, mime_type, label=""):
    """使用 Gemini 分析文本或多模态附件"""
    if not GEMINI_API_KEY:
        return []
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
这是一份大学工程系课程【{label}】的通知、网页正文、作业大纲、编程实验安排或考核计划（当前学年为 2026/2027 学年，今天是 2026 年 10 月 25 日）。
请仔细提取属于【KKTSS / Engineering Campus / EE Engineering】的所有明确截止时间与任务功课（严格忽略 Main Campus / Induk）：

【必须提取的功课与考核范围（只要有明确截止时间或提交要求，一律算作必须完成的功课/待办）】：
1. 编程实验 / Lab / Hands-on 任务：
   - 例如：Lab 1 (包括 Lab 1A, Lab 1B, Lab 1C, Lab 1D 等全部子任务)、Lab 2、Lab 3...
   - 包含外部代码提交平台（如 classroom50.org、GitHub Classroom 等）。
   - 注意：如果正文提到后续 Lab 的截止规律（如“For the next labs, the deadline will be on the same day, before 1700”），请将当前有确切日期的 Lab 1 完整提取，并在 description 中注明后续 lab 的常规提交时间规则！
2. Assignment 作业、Tugasan、Coursework、Mini Project。
3. Online Quiz、Kuiz、Test (如 Test 1, Test 2)、Mid-term Exam、Final Examination。
4. Weekly Homework、Tutorial 提交任务、Amali 实验报告。

时间与链接规范：
1. 截止时间转换规则（非常重要）：
   - “DEADLINE: 26 October 2026, 1159” -> 2026-10-26T11:59:00
   - “before 1700” -> 17:00:00
   - “8-10 PM” -> 20:00:00
   - 只有日期没有写具体小时分秒的，默认设为当天 23:59:00。
2. 严格时间范围：所有事件必须在 2026 年 10 月至 2027 年 3 月之间，严禁输出 2030 年！
3. 外部链接：如果在正文或附带的链接中有提交地址（如 classroom50.org 或 github），务必完整保留在 description 字段中，方便学生一键点击直达！

请以严格规范的 JSON 输出：
{{
  "events": [
    {{
      "summary": "【{label}】任务名称 (例: 【{label}】Lab 1 (Lab 1A-1D) 截止 或 【{label}】Assignment 1 截止)",
      "description": "提交方式/外部链接/考场地点/备考说明 (例: 提交链接: https://classroom50.org/... | DEADLINE: 26 Oct 1159)",
      "start_iso": "YYYY-MM-DDTHH:MM:SS",
      "end_iso": "YYYY-MM-DDTHH:MM:SS"
    }}
  ]
}}
"""
    contents = []
    if mime_type == "text/plain":
        contents.append(f"【课程网页/正文内容】:\n{content_data}\n\n{prompt}")
    else:
        contents.append(types.Part.from_bytes(data=content_data, mime_type=mime_type))
        contents.append(prompt)

    candidate_models = [MODEL_NAME, "gemini-3.1-flash-lite", "gemini-flash-latest"]
    for m in candidate_models:
        try:
            response = client.models.generate_content(
                model=m,
                contents=contents,
                config={"response_mime_type": "application/json"}
            )
            data = json.loads(response.text)
            raw_events = data.get("events", [])
            return filter_future_events(raw_events)
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                continue
            print(f"[WARN] AI 解析失败 [{label}]: {e}")
            return []
    return []

def deduplicate_and_reconcile_events(events_list, course_name):
    """解决信息冲突：当同一门课有粗略周次估算与官方具体通知冲突时，优先保留官方确切时间与地点"""
    if len(events_list) <= 1 or not GEMINI_API_KEY:
        return events_list

    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
以下是从【{course_name}】抓取到的所有待办与考试事件（可能包含周次进度图估算时间与正式通知时间重叠、冲突的情况，例如 Test 1 出现了不同日期）：
请进行去重与冲突仲裁：
1. 如果同一个事件（如同一个 Test 1 或同一个 Assignment 1）有多个不同时间，请优先保留带有明确具体时间段（如 8-10 PM）、明确考场地点（如 DU & DSG、DK）或明确截止时间的官方精准条目，剔除模棱两可的粗略条目。
2. 绝对不能误删不同编号的任务！例如 Assignment 1 和 Assignment 2 都要保留；Lab 1, Lab 2, Lab 3 等属于不同阶段的功课，必须全部独立保留！
3. 严格剔除日期早于今天（2026-10-25）的历史事件以及年份错误的条目（如 2030 年）。

待处理事件列表:
{json.dumps(events_list, ensure_ascii=False, indent=2)}

请以严格 JSON 格式返回仲裁合并后的最终列表：
{{
  "events": [
    {{
      "summary": "最终任务名称",
      "description": "地点、提交链接与细节",
      "start_iso": "YYYY-MM-DDTHH:MM:SS",
      "end_iso": "YYYY-MM-DDTHH:MM:SS"
    }}
  ]
}}
"""
    try:
        res = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
        return json.loads(res.text).get("events", events_list)
    except Exception as e:
        print(f"[WARN] 冲突仲裁异常: {e}，保留原列表")
        return events_list

# ================= 2. 爬虫流程 =================
def run_scraper(seen_ids):
    all_events = []
    notifications = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            viewport={"width": 1280, "height": 850},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0"
        )
        page = context.new_page()

        print("[INFO] 正在访问 USM e-Learn 登录页面...")
        page.goto("https://elearning.usm.my/sidang2627/login/index.php", timeout=60000)
        page.wait_for_timeout(2000)

        # 寻找 ADFS 入口
        print("[INFO] 正在定位 ADFS 登录入口...")
        sso_btn = page.locator("a[href*='login.usm.my'], a[href*='adfs']").first
        sso_btn.wait_for(state="attached", timeout=15000)
        target_url = sso_btn.get_attribute("href")
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        # 输入账号密码
        print("[INFO] 正在输入 ADFS 凭据...")
        page.wait_for_selector("input[type='password']", timeout=20000)
        user_input = page.locator("#userNameInput, input[name='UserName'], input[type='email'], input[type='text']").first
        user_input.fill(USM_USERNAME)
        pass_input = page.locator("#passwordInput, input[name='Password'], input[type='password']").first
        pass_input.fill(USM_PASSWORD)

        submit_btn = page.locator("#submitButton, input[type='submit'], button[type='submit']").first
        if submit_btn.is_visible():
            submit_btn.click()
        else:
            page.keyboard.press("Enter")

        # 等待重定向
        print("[INFO] 正在等待 ADFS 认证回跳...")
        try:
            page.wait_for_url("**/elearning.usm.my/**", timeout=15000)
        except Exception:
            err_elem = page.query_selector("#errorText, .error")
            if err_elem and err_elem.inner_text().strip():
                print(f"\n❌ 登录失败！ADFS 提示: 【{err_elem.inner_text().strip()}】\n")
                browser.close()
                return [], []

        page.wait_for_timeout(3000)
        print(f"[INFO] 登录成功！当前所在页面: {page.url}")

        # 深入抓取 Semester I 全部课程
        try:
            page.goto("https://elearning.usm.my/sidang2627/my/", timeout=30000)
            page.wait_for_timeout(2000)

            raw_links = page.query_selector_all("a[href*='/course/view.php?id=']")
            course_map = {}
            for a in raw_links:
                href = a.get_attribute("href")
                title = a.inner_text().strip()
                if href and title and len(title) > 3 and "view all" not in title.lower():
                    clean_url = href.split("&")[0] if "&" in href else href
                    if clean_url not in course_map:
                        course_map[clean_url] = title

            print(f"[INFO] 成功识别到 Semester I 共 {len(course_map)} 门科目，开始逐一深入扫描...")

            for idx, (c_url, course_title) in enumerate(course_map.items(), 1):
                try:
                    code_match = re.search(r'([A-Z]{3}\d{3})', course_title)
                    c_code = code_match.group(1) if code_match else course_title[:10]

                    print(f"\n[INFO] 正在扫描科目 ({idx}/{len(course_map)}): 【{c_code} - {course_title}】...")
                    # 使用 domcontentloaded 秒级加载，不傻等外部无关插件
                    page.goto(c_url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(1000)

                    course_events = []

                    # --- 1. 扫描网页内嵌正文、作业列表与通知表格 ---
                    try:
                        main_elem = page.query_selector("#region-main, .course-content, [role='main']")
                        page_text = main_elem.inner_text() if main_elem else page.inner_text("body")

                        # 提取课程页面中的作业/实验链接（如 GitHub Classroom, Classroom50, Google Forms, 提交入口等）
                        link_snippets = []
                        links = page.query_selector_all("#region-main a, .course-content a, a[href*='classroom'], a[href*='github'], a[href*='assign']")
                        for a in links:
                            try:
                                href = a.get_attribute("href") or ""
                                txt = a.inner_text().strip()
                                if href and any(k in href.lower() for k in ["classroom", "github", "form", "assign", "submission"]):
                                    link_snippets.append(f"- [{txt}]: {href}")
                            except Exception:
                                pass

                        if link_snippets:
                            page_text += "\n\n【页面提取到的关键作业/提交链接】:\n" + "\n".join(link_snippets[:20])

                        keywords = ["deadline", "assessment", "important dates", "test", "quiz", "kuiz", "tugasan", "tarikh akhir", "lab", "amali", "praktikal", "classroom", "assignment", "submission", "due date"]
                        if any(kw in page_text.lower() for kw in keywords):
                            text_id = hashlib.md5(f"text_{c_code}_{page_text[:1000]}".encode()).hexdigest()
                            if text_id not in seen_ids:
                                print(f"  📝 发现课程正文重要通知/作业列表，正在分析...")
                                evs = analyze_content_with_ai(page_text[:15000], "text/plain", c_code)
                                course_events.extend(evs)
                                seen_ids.add(text_id)
                    except Exception as te:
                        print(f"  [WARN] 正文分析异常: {te}")

                    # --- 2. 扫描 PDF 大纲文件 ---
                    pdf_anchors = page.query_selector_all("a[href*='resource/view.php'], a[href*='mod/assign'], a[href*='pluginfile.php']")
                    for a in pdf_anchors:
                        txt = a.inner_text().strip()
                        href = a.get_attribute("href")
                        if href and is_relevant_for_user(txt) and any(kw in txt.lower() for kw in PLAN_KEYWORDS):
                            link_id = hashlib.md5(href.encode()).hexdigest()
                            if link_id not in seen_ids:
                                print(f"  📄 发现匹配大纲/作业文件: {txt}，下载解析中...")
                                try:
                                    res = page.request.get(href)
                                    if "pdf" in res.headers.get("content-type", ""):
                                        evs = analyze_content_with_ai(res.body(), "application/pdf", f"{c_code} - {txt}")
                                        course_events.extend(evs)
                                        seen_ids.add(link_id)
                                except Exception as ex:
                                    print(f"  [WARN] PDF 处理异常: {ex}")

                    # --- 3. 扫描里程碑 Banner 进度图 ---
                    images = page.query_selector_all("img")
                    for img in images:
                        src = img.get_attribute("src")
                        alt = (img.get_attribute("alt") or "").strip()
                        if src and is_relevant_for_user(alt) and any(kw in (alt + src).lower() for kw in ["milestone", "assessment", "plan"]):
                            img_id = hashlib.md5(src.encode()).hexdigest()
                            if img_id not in seen_ids:
                                print(f"  🖼️ 发现进度图: {alt or 'Banner'}，解析中...")
                                try:
                                    res = page.request.get(src)
                                    evs = analyze_content_with_ai(res.body(), "image/png", f"{c_code} Banner")
                                    course_events.extend(evs)
                                    seen_ids.add(img_id)
                                except Exception as ex:
                                    print(f"  [WARN] 图片分析异常: {ex}")

                    # --- 4. 智能冲突仲裁与去重 ---
                    if course_events:
                        print(f"  ⚖️ 正在为【{c_code}】进行时间消重与冲突仲裁 (原始 {len(course_events)} 条)...")
                        reconciled = deduplicate_and_reconcile_events(course_events, c_code)
                        print(f"  ✅ 仲裁完毕，保留最终确定条目: {len(reconciled)} 个！")
                        all_events.extend(reconciled)

                except Exception as ce:
                    print(f"  [WARN] 扫描科目【{course_title}】时遇到网络等待，已记录并平滑继续下一门: {ce}")
                    continue

        except Exception as e:
            print(f"[WARN] 课程遍历异常: {e}")

        browser.close()

    return all_events, notifications

# ================= 3. 主程序入口 =================
def main():
    state = load_state()
    seen_ids = set(state.get("seen_ids", []))
    is_first_run = not state.get("initialized", False)

    print(f"[INFO] 启动运行 | 模式: {'【首次运行初始化】' if is_first_run else '【日常定时增量巡检】'}")
    all_events, notifications = run_scraper(seen_ids)

    valid_events = filter_future_events(all_events)

    if is_first_run:
        print(f"[INFO] 首次运行：整理出 {len(valid_events)} 个未来关键时间节点...")
        if valid_events:
            os.makedirs("data", exist_ok=True)
            with open("data/events.json", "w", encoding="utf-8") as f:
                json.dump(valid_events, f, ensure_ascii=False, indent=2)

            # 按科目代码分组
            course_grouped = {}
            for ev in valid_events:
                summary = ev.get('summary', '')
                code_match = re.search(r'([A-Z]{3}\d{3})', summary)
                course_code = code_match.group(1) if code_match else "综合任务"
                course_grouped.setdefault(course_code, []).append(ev)

            lines = []
            for c_code, ev_list in course_grouped.items():
                lines.append(f"📚 <b>【{html.escape(c_code)}】</b>")
                for ev in ev_list:
                    summary = html.escape(ev.get('summary', ''))
                    desc = f" ({html.escape(ev.get('description', ''))})" if ev.get('description') else ""
                    time_str = ev.get('start_iso', '')[:16].replace('T', ' ')
                    lines.append(f"  • {summary}: <code>{time_str}</code>{desc}")
                lines.append("")

            events_text = "\n".join(lines).strip()
            send_telegram(
                f"🎓 <b>【USM e-Learn 智能助理初始化完成】</b>\n"
                f"📍 <b>校区定位</b>：Engineering Campus / KKTSS (EE)\n"
                f"🗓️ <b>未来重要日程总览（已仲裁消重）：</b>\n\n"
                f"{events_text}\n\n"
                f"💡 <i>后续将每 30 分钟静默巡检，只有新通知/新变动才会提醒你。</i>"
            )
            # 自动同步至 Google 日历
            sync_to_google_calendar(valid_events)
            state["initialized"] = True
        else:
            print("[INFO] 本次未提取到日程，不锁定初始化状态，以便修正后重跑。")

    else:
        if valid_events:
            print(f"[INFO] 发现新重要时间 {len(valid_events)} 个...")
            lines = []
            for ev in valid_events:
                summary = html.escape(ev.get('summary', ''))
                time_str = ev.get('start_iso', '')[:16].replace('T', ' ')
                desc = f" ({html.escape(ev.get('description', ''))})" if ev.get('description') else ""
                lines.append(f"• <b>{summary}</b>: <code>{time_str}</code>{desc}")
            events_text = "\n".join(lines)
            send_telegram(f"⚡ <b>发现新考试/作业截止时间提醒：</b>\n\n{events_text}")
            sync_to_google_calendar(valid_events)

    state["seen_ids"] = list(seen_ids)[-1000:]
    save_state(state)
    print("[SUCCESS] 运行完毕，状态已保存。")

if __name__ == "__main__":
    main()
