#!/usr/bin/env python3
"""验证 volfree 三态原型：交互流程 + 无 JS 错误 + 文字对比度，并导出 2x 截图。

用法：
    /Users/xushuda/.workbuddy/binaries/python/envs/default/bin/python design/check_design.py

依赖 playwright（走系统 Chrome，无需下载内核）与 Pillow，仅存在于国内版 venv。
截图直接写入 design/shots/，对比度用「先把全页文字设为透明再取样背景」的方式测量，
避免取样点落在字形上导致的假数据。
"""

import pathlib
import sys
import tempfile
from playwright.sync_api import sync_playwright
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
HTML = HERE / "popover.html"
OUT = HERE / "shots"
OUT.mkdir(parents=True, exist_ok=True)

checks = []


def ck(name, cond, detail=""):
    checks.append((name, bool(cond), detail))


# ---------- WCAG 相对亮度 ----------
def _lin(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum(rgb):
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ratio(fg, bg):
    l1, l2 = lum(fg), lum(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def parse_rgba(s):
    s = s.strip()
    if s.startswith("rgba"):
        nums = s[s.index("(") + 1: s.rindex(")")].split(",")
        return tuple(float(n) for n in nums[:3]) + (float(nums[3]),)
    nums = s[s.index("(") + 1: s.rindex(")")].split(",")
    return tuple(float(n) for n in nums[:3]) + (1.0,)


def over(fg, bg):
    r, g, b, a = fg
    return (a * r + (1 - a) * bg[0], a * g + (1 - a) * bg[1], a * b + (1 - a) * bg[2])


# 需要检查的 (选择器, 说明, 是否大字号)
TARGETS = [
    ("#vtext", "裁决标题", True),
    ("#vreason", "裁决理由", False),
    (".vol-name", "盘名", False),
    (".vol-meta", "容量格式", False),
    ("#primary", "主按钮", False),
    ("#dlabel", "详情入口", False),
    (".notes h2", "说明栏标题", False),
]


def measure(page, label):
    """把全页文字变透明后截一张纯背景图，再逐元素合成算对比度。"""
    colors = {}
    boxes = {}
    for sel, name, _ in TARGETS:
        el = page.query_selector(sel)
        if not el or not el.is_visible():
            continue
        colors[sel] = page.eval_on_selector(sel, "el => getComputedStyle(el).color")
        boxes[sel] = el.bounding_box()

    # 额外的行内小元素
    for sel, name in [
        (".proc .tg.warn", "已删除·写入中 标签"),
        (".proc .tg.mark", "已删除·只读 标签"),
        (".notes li", "说明条目"),
        ("#pconfirmQ", "软确认文案"),
        ("#pok", "仍然结束 按钮"),
        ("#pcancel", "取消 按钮"),
        (".end", "结束… 链接"),
    ]:
        el = page.query_selector(sel)
        if el and el.is_visible():
            colors[sel] = page.eval_on_selector(sel, "el => getComputedStyle(el).color")
            boxes[sel] = el.bounding_box()

    page.add_style_tag(content="*{ color: transparent !important; }")
    page.wait_for_timeout(60)
    shot = pathlib.Path(tempfile.mkdtemp()) / "bg_probe.png"
    page.screenshot(path=str(shot), full_page=True)
    page.evaluate("() => { const s=[...document.querySelectorAll('style')].pop(); s.remove(); }")
    page.wait_for_timeout(40)

    img = Image.open(shot).convert("RGB")
    scale = img.width / page.evaluate("() => document.documentElement.clientWidth")

    results = []
    for sel, box in boxes.items():
        if not box:
            continue
        x = int((box["x"] + box["width"] / 2) * scale)
        y = int((box["y"] + box["height"] / 2) * scale)
        x = max(0, min(img.width - 1, x))
        y = max(0, min(img.height - 1, y))
        bg = img.getpixel((x, y))
        fg = over(parse_rgba(colors[sel]), bg)
        results.append((sel, colors[sel], bg, ratio(fg, bg)))
    return results


def shot_stage(page, name):
    page.mouse.move(2, 2)
    page.wait_for_timeout(120)
    box = page.locator(".stage").bounding_box()
    pad = 26
    clip = {
        "x": max(0, box["x"] - pad),
        "y": max(0, box["y"] - pad),
        "width": box["width"] + pad * 2,
        "height": box["height"] + pad * 2,
    }
    page.screenshot(path=str(OUT / name), clip=clip)


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")

        # ============ 明色 ============
        ctx = browser.new_context(viewport={"width": 880, "height": 820},
                                  device_scale_factor=2, color_scheme="light")
        page = ctx.new_page()
        jserr = []
        page.on("pageerror", lambda e: jserr.append(str(e)))
        page.on("console", lambda m: jserr.append("console:" + m.text) if m.type == "error" else None)
        page.goto(HTML.as_uri())
        page.wait_for_timeout(250)

        # --- 初始：黄态 ---
        ck("初始为黄态", page.inner_text("#vtext") == "可以推出", page.inner_text("#vtext"))
        ck("理由提到已删除临时文件", "已删除的临时文件" in page.inner_text("#vreason"),
           page.inner_text("#vreason"))
        ck("主按钮为「结束并推出」", page.inner_text("#primary") == "结束并推出",
           page.inner_text("#primary"))
        ck("软确认初始隐藏", not page.is_visible("#pconfirm"))
        ck("说明栏标题标明会放弃写入",
           "会放弃写入" in page.inner_text("#notesTitle"), page.inner_text("#notesTitle"))

        # --- 点主按钮 → 软确认 ---
        page.click("#primary")
        page.wait_for_timeout(180)
        ck("点主按钮弹出软确认", page.is_visible("#pconfirm"))
        ck("软确认文案给出次数", "放弃 1 次写入" in page.inner_text("#pconfirmQ"),
           page.inner_text("#pconfirmQ"))
        ck("焦点落在「取消」而非破坏性按钮",
           page.evaluate("() => document.activeElement.id") == "pcancel",
           page.evaluate("() => document.activeElement.id"))
        ck("主按钮 aria-expanded=true",
           page.get_attribute("#primary", "aria-expanded") == "true")
        ck("此时尚未推出", page.inner_text("#primary") == "结束并推出", page.inner_text("#primary"))
        shot_stage(page, "03-黄-软确认.png")

        # --- 取消 ---
        page.click("#pcancel")
        page.wait_for_timeout(180)
        ck("取消后软确认收起", not page.is_visible("#pconfirm"))
        ck("取消后焦点回到主按钮",
           page.evaluate("() => document.activeElement.id") == "primary",
           page.evaluate("() => document.activeElement.id"))

        # --- Esc 也能收起 ---
        page.click("#primary")
        page.wait_for_timeout(120)
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        ck("Esc 收起软确认", not page.is_visible("#pconfirm"))

        # --- 展开进程，逐项结束临时文件 ---
        page.click("#disclose")
        page.wait_for_timeout(400)
        ck("展开后有 3 个进程", page.locator(".plist .proc").count() == 3,
           str(page.locator(".plist .proc").count()))
        ck("写入中的临时文件也有「结束…」入口",
           page.locator(".proc .end").count() == 1,
           str(page.locator(".proc .end").count()))
        shot_stage(page, "02-黄-展开进程.png")

        page.click(".proc .end")
        page.wait_for_timeout(200)
        ck("逐项确认用软底色（黄态）",
           page.eval_on_selector(".plist .confirm", "el => el.classList.contains('soft')"))
        ck("逐项确认文案说明无法找回",
           "无法找回" in page.inner_text(".plist .confirm .q"),
           page.inner_text(".plist .confirm .q"))
        shot_stage(page, "04-黄-逐项确认.png")

        page.click(".plist .confirm [data-cancel]")
        page.wait_for_timeout(150)

        # --- 结束掉那个写临时文件的进程：应保持展开，且降级为「可以安全推出」---
        page.click(".proc .end")
        page.wait_for_timeout(150)
        page.click(".plist .confirm [data-end]")
        page.wait_for_timeout(300)
        ck("黄态结束一个进程后仍保持展开",
           page.locator(".plist .proc").count() == 2,
           str(page.locator(".plist .proc").count()))
        ck("黄态结束后不再有「写入中」标签",
           page.locator(".proc .tg.warn").count() == 0,
           str(page.locator(".proc .tg.warn").count()))
        ck("黄态结束后降级为「可以安全推出」",
           page.inner_text("#vtext") == "可以安全推出", page.inner_text("#vtext"))
        ck("降级后主按钮不再需要软确认",
           page.inner_text("#primary") == "安全结束并推出", page.inner_text("#primary"))

        # --- 重置回黄态，完整走一遍：确认 → 推出 ---
        page.click('.switcher button[data-state="caution"]')
        page.wait_for_timeout(250)
        page.click("#primary")
        page.wait_for_timeout(150)
        page.click("#pok")
        page.wait_for_timeout(900)
        ck("确认后进入已推出", page.inner_text("#vtext") == "已推出", page.inner_text("#vtext"))
        ck("已推出后主按钮禁用", page.is_disabled("#primary"))

        # --- 绿态 ---
        page.click('.switcher button[data-state="safe"]')
        page.wait_for_timeout(300)
        ck("绿态主按钮为「推出」", page.inner_text("#primary") == "推出", page.inner_text("#primary"))
        ck("绿态详情入口隐藏", not page.is_visible("#disclose"))
        ck("绿态无软确认残留", not page.is_visible("#pconfirm"))
        shot_stage(page, "01-绿-可以拔了.png")

        # --- 红态 ---
        page.click('.switcher button[data-state="danger"]')
        page.wait_for_timeout(300)
        ck("红态主按钮禁用", page.is_disabled("#primary"))
        ck("红态主按钮为「等待完成」", page.inner_text("#primary") == "等待完成",
           page.inner_text("#primary"))
        ck("红态强制入口改名", page.inner_text("#dlabel") == "仍要结束…",
           page.inner_text("#dlabel"))
        page.click("#disclose")
        page.wait_for_timeout(400)
        shot_stage(page, "05-红-展开进程.png")
        page.click(".proc .end")
        page.wait_for_timeout(200)
        ck("红态逐项确认用警示底色",
           not page.eval_on_selector(".plist .confirm", "el => el.classList.contains('soft')"))
        shot_stage(page, "06-红-逐项确认.png")

        ck("明色无 JS 错误", not jserr, " | ".join(jserr))
        ctx.close()

        # ============ 暗色 ============
        ctx2 = browser.new_context(viewport={"width": 880, "height": 820},
                                   device_scale_factor=2, color_scheme="dark")
        pg = ctx2.new_page()
        err2 = []
        pg.on("pageerror", lambda e: err2.append(str(e)))
        pg.goto(HTML.as_uri())
        pg.wait_for_timeout(250)
        pg.click("#disclose")
        pg.wait_for_timeout(400)
        shot_stage(pg, "07-深色-黄-展开.png")

        pg.click('.switcher button[data-state="danger"]')
        pg.wait_for_timeout(250)
        pg.click("#disclose")
        pg.wait_for_timeout(400)
        shot_stage(pg, "08-深色-红-展开.png")
        ck("暗色无 JS 错误", not err2, " | ".join(err2))

        ctx2.close()

        # ============ 对比度（明/暗 × 三态）============
        contrast_report = []
        for scheme in ("light", "dark"):
            c = browser.new_context(viewport={"width": 880, "height": 820},
                                    device_scale_factor=2, color_scheme=scheme)
            pp = c.new_page()
            pp.goto(HTML.as_uri())
            pp.wait_for_timeout(200)
            for state in ("safe", "caution", "danger"):
                pp.click('.switcher button[data-state="%s"]' % state)
                pp.wait_for_timeout(150)
                if state != "safe":
                    pp.click("#disclose")
                    pp.wait_for_timeout(350)
                if state == "caution":
                    pp.click("#primary")
                    pp.wait_for_timeout(150)
                contrast_report.append((scheme, state, measure(pp, state)))
            c.close()

        browser.close()

    # ---------- 输出 ----------
    print("=" * 72)
    print("交互与状态校验")
    print("=" * 72)
    bad = 0
    for name, ok, detail in checks:
        print(("  ✅ " if ok else "  ❌ ") + name + (("  → " + detail) if detail and not ok else ""))
        if not ok:
            bad += 1

    print()
    print("=" * 72)
    print("文字对比度（WCAG AA：正文 ≥4.5，大字号 ≥3.0）")
    print("=" * 72)
    fails = 0
    for scheme, state, rows in contrast_report:
        print("\n[%s / %s]" % (scheme, state))
        for sel, fg, bg, r in rows:
            big = any(sel == t[0] and t[2] for t in TARGETS)
            need = 3.0 if big else 4.5
            ok = r >= need
            if not ok:
                fails += 1
            print("  %s %-22s %5.2f:1  (需 %.1f)  fg=%s bg=rgb%s"
                  % ("✅" if ok else "❌", sel, r, need, fg,
                     tuple(int(v) for v in bg)))

    print()
    print("截图 → %s" % OUT)
    for f in sorted(OUT.glob("*.png")):
        if f.name.startswith("_"):
            continue
        print("  %s  %dx%d  %.0f KB" % (f.name, *Image.open(f).size, f.stat().st_size / 1024))

    print()
    print("失败项：交互 %d，对比度 %d" % (bad, fails))
    return 1 if (bad or fails) else 0


if __name__ == "__main__":
    sys.exit(main())
