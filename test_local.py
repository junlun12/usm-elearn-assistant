import os
import sys
import time
from playwright.sync_api import sync_playwright

print("=" * 60)
print("🎓 USM e-Learn 本地可视化测试脚本")
print("=" * 60)

# 优先从环境变量或用户输入获取账号密码
USM_USERNAME = os.environ.get("USM_USERNAME")
USM_PASSWORD = os.environ.get("USM_PASSWORD")

if not USM_USERNAME:
    USM_USERNAME = input("请输入你的 USM 学生邮箱 (例如 XXXX@student.usm.my): ").strip()
if not USM_PASSWORD:
    import getpass
    USM_PASSWORD = getpass.getpass("请输入你的 USM 登录密码 (输入时不显示字符): ").strip()

print("\n[1/4] 正在启动本地可视化浏览器 (headless=False)...")

with sync_playwright() as p:
    # 优先直接调用系统自带的 Edge / Chrome 浏览器，秒速启动无需额外下载
    try:
        browser = p.chromium.launch(channel="msedge", headless=False, slow_mo=800)
    except Exception:
        browser = p.chromium.launch(headless=False, slow_mo=800)
    context = browser.new_context(viewport={"width": 1280, "height": 850})
    page = context.new_page()

    print("[2/4] 正在打开 e-Learn 登录页: https://elearning.usm.my/sidang2627/login/index.php")
    page.goto("https://elearning.usm.my/sidang2627/login/index.php", timeout=60000)
    page.wait_for_timeout(2000)

    current_url = page.url
    print(f"[DEBUG] 当前页面: {current_url} | 标题: {page.title()}")

    # 寻找并点击 SSO 链接
    if "login.usm.my" not in current_url.lower():
        print("[INFO] 正在寻找并点击 ADFS / Identity 登录入口...")
        try:
            sso_btn = page.locator("a[href*='login.usm.my'], a[href*='adfs']").first
            sso_btn.wait_for(state="attached", timeout=15000)
            target_url = sso_btn.get_attribute("href")
            print(f"[INFO] 找到目标跳转地址: {target_url[:80]}...")
            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[WARN] 直接获取链接失败: {e}，尝试模拟点击...")
            page.locator("a[href*='login.usm.my'], a[href*='adfs']").first.click()

    page.wait_for_timeout(2000)
    print(f"[DEBUG] 已到达认证页面: {page.url} | 标题: {page.title()}")

    print("[3/4] 正在自动输入账号与密码...")
    page.wait_for_selector("input[type='password']", timeout=30000)

    # 填充账号
    user_input = page.locator("#userNameInput, input[name='UserName'], input[type='email'], input[type='text']").first
    user_input.fill(USM_USERNAME)

    # 填充密码
    pass_input = page.locator("#passwordInput, input[name='Password'], input[type='password']").first
    pass_input.fill(USM_PASSWORD)

    print("[INFO] 正在点击登录提交...")
    submit_btn = page.locator("#submitButton, input[type='submit'], button[type='submit']").first
    if submit_btn.is_visible():
        submit_btn.click()
    else:
        page.keyboard.press("Enter")

    print("[4/4] 登录已提交，等待回跳 e-Learn Dashboard...")
    page.wait_for_timeout(6000)

    print(f"\n🎉 当前所在页面: {page.url} | 标题: {page.title()}")
    if "my" in page.url or "sidang2627" in page.url:
        print("✅ 恭喜！本地自动化登录完全成功！")
        
        # 尝试抓取待办事件
        print("\n🔍 正在查看待办与日历列表...")
        page.goto("https://elearning.usm.my/sidang2627/calendar/view.php?view=upcoming", timeout=30000)
        page.wait_for_timeout(2000)
        events = page.query_selector_all(".eventnode, .calendar-event")
        print(f"📌 发现近期事件数量: {len(events)}")
        for i, ev in enumerate(events[:5], 1):
            name = ev.query_selector(".name, h3")
            d = ev.query_selector(".date")
            print(f"  {i}. {name.inner_text().strip() if name else ''} | {d.inner_text().strip() if d else ''}")

    print("\n[提示] 保持浏览器打开 10 秒供你查看，然后自动退出...")
    page.wait_for_timeout(10000)
    browser.close()
    print("测试完毕。")
