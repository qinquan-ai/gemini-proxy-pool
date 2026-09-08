"""
Studio Inspector: Launches visible Chrome, navigates to Google AI Studio rate-limit dashboard,
handles automated 'See more' expansion, smooth scrolling, dynamic DOM metric extraction,
and captures high-resolution snapshots for manual calibration.
"""

import os
import sys
import time
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from playwright.sync_api import sync_playwright

DEFAULT_AI_STUDIO_URL = "https://aistudio.google.com/rate-limit?timeRange=last-1-day&project=gen-lang-client-0386075480"
DEFAULT_PROFILE = "Default"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
STUDIO_PROFILE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "studio_chrome_profile"
SNAPSHOTS_DIR = Path(__file__).resolve().parent.parent.parent / "logs" / "snapshots"


def find_system_chrome() -> str:
    candidates = [
        CHROME_PATH,
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return CHROME_PATH


class StudioInspector:
    def __init__(
        self,
        profile_name: str | int = DEFAULT_PROFILE,
        target_url: str = DEFAULT_AI_STUDIO_URL,
        port: int = 9222,
    ):
        self.profile_name = str(profile_name)
        self.target_url = target_url
        self.port = port
        self.profile_dir = STUDIO_PROFILE_DIR

    def _ensure_all_models_toggle(self, page) -> None:
        """Ensure 'All models' toggle is checked without accidentally toggling it off."""
        print("[*] 正在检查 'All models' 开关状态...")
        try:
            status = page.evaluate("""() => {
                const toggle = document.querySelector('mat-slide-toggle[data-test-toggle="toggle-view-all-models"], mat-slide-toggle');
                if (!toggle) return null;
                const btn = toggle.querySelector('button[role="switch"]');
                const checked = toggle.classList.contains('mat-mdc-slide-toggle-checked') || 
                                (btn && btn.getAttribute('aria-checked') === 'true') ||
                                (btn && btn.classList.contains('mdc-switch--checked'));
                return { found: true, checked: !!checked };
            }""")

            if status and status.get("found"):
                if status.get("checked"):
                    print("  [✓] 'All models' 开关已处于开启状态，完整模型列表已激活。")
                else:
                    print("  [+] 'All models' 开关当前未开启，正在主动开启...")
                    toggle_btn = page.locator("mat-slide-toggle[data-test-toggle='toggle-view-all-models'] button[role='switch'], mat-slide-toggle button[role='switch']").first
                    toggle_btn.click(timeout=3000)
                    time.sleep(2.0)
            else:
                print("  [!] 未找到开关组件，尝试等待渲染...")
        except Exception as e:
            print(f"  [!] 检查开关时出现异常: {e}")

    def _extract_dynamic_dom_elements(self, page) -> List[Dict[str, Any]]:
        """Extract live model quota metrics into clean structured table data with exact DOM classes."""
        print("\n[*] 正在提取页面动元素（全量模型实时配额与使用率）...")
        parsed_rows = page.evaluate("""() => {
            const results = [];
            const rows = document.querySelectorAll('tr[mat-row], tr.mat-mdc-row');
            
            rows.forEach(r => {
                // 1. Status icon & health
                const statusBtn = r.querySelector('td.cdk-column-Status button');
                const isWarning = statusBtn && (
                    statusBtn.classList.contains('warning') || 
                    statusBtn.getAttribute('aria-label') === 'Close to limit' ||
                    (statusBtn.innerText || '').includes('warning')
                );
                const status = isWarning ? '⚠️ 接近上限' : '✅ 充足';

                // 2. Model Name
                const modelEl = r.querySelector('td.cdk-column-Model .model-cell span') || 
                                r.querySelector('td.cdk-column-Model .model-cell') ||
                                r.querySelector('td.cdk-column-Model');
                let model = (modelEl ? modelEl.innerText : '').trim().replace(/\\n/g, ' ');
                const testId = r.querySelector('td.cdk-column-Model')?.getAttribute('data-test-id') || '';

                // 3. Category
                const catEl = r.querySelector('td.cdk-column-Category .category') || 
                              r.querySelector('td.cdk-column-Category');
                const category = (catEl ? catEl.innerText : '').trim().replace(/\\n/g, ' ');

                // 4. RPM
                const rpmEl = r.querySelector('td.cdk-column-RPM .metric-value') || 
                              r.querySelector('td.cdk-column-RPM');
                const rpm = (rpmEl ? rpmEl.innerText : '-').trim().replace(/\\s+/g, ' ');

                // 5. TPM
                const tpmEl = r.querySelector('td.cdk-column-TPM .metric-value') || 
                              r.querySelector('td.cdk-column-TPM');
                const tpm = (tpmEl ? tpmEl.innerText : '-').trim().replace(/\\s+/g, ' ');

                // 6. RPD
                const rpdEl = r.querySelector('td.cdk-column-RPD .metric-value') || 
                              r.querySelector('td.cdk-column-RPD');
                const rpd = (rpdEl ? rpdEl.innerText : '-').trim().replace(/\\s+/g, ' ');

                if (model && !model.toLowerCase().includes('model')) {
                    results.push({
                        status,
                        model,
                        test_id: testId,
                        category,
                        rpm,
                        tpm,
                        rpd,
                    });
                }
            });
            return results;
        }""")

        # Deduplicate by model name
        unique_models = []
        seen = set()
        for r in parsed_rows:
            m = r.get("model")
            if m and m not in seen:
                seen.add(m)
                unique_models.append(r)

        if unique_models:
            print("\n" + "=" * 98)
            print(f" 📊  Google AI Studio 实时各模型配额与用量看板 (共识别到 {len(unique_models)} 个模型)")
            print("=" * 98)
            print(f" {'序号':<4} | {'状态':<10} | {'模型名称 (Model)':<28} | {'分类 (Category)':<18} | {'RPM':<12} | {'RPD':<12}")
            print("-" * 98)
            for idx, item in enumerate(unique_models, 1):
                stat = item.get("status", "")
                m_name = item.get("model", "")[:28]
                cat = item.get("category", "")[:18]
                rpm = item.get("rpm", "")[:12]
                rpd = item.get("rpd", "")[:12]
                print(f"  [{idx:>2}] | {stat:<10} | {m_name:<28} | {cat:<18} | {rpm:<12} | {rpd:<12}")
            print("=" * 98 + "\n")
        else:
            print("[!] 当前 DOM 中未捕获到行元素，请确认页面已完全加载。")

        return unique_models

    def _capture_stitched_long_screenshot(self, page, final_path: Path) -> str:
        """
        Captures a seamless, full-height stitched long screenshot (similar to WeChat long screenshot),
        scrolling the true container (.page-content-wrapper) to eliminate duplicate sticky sidebars
        and perfectly adapting to any screen resolution, DPR, or display scaling.
        """
        import io
        from PIL import Image

        print("[*] 正在执行微信式智能截长屏 (自适应真实容器滚动与无缝拼接)...")

        wrapper_locator = page.locator('.page-content-wrapper')
        if wrapper_locator.count() == 0:
            wrapper_locator = page

        # Ensure container is at top
        page.evaluate("""() => {
            const el = document.querySelector('.page-content-wrapper');
            if (el) el.scrollTop = 0;
            window.scrollTo(0, 0);
        }""")
        time.sleep(1.0)

        metrics = page.evaluate("""() => {
            const el = document.querySelector('.page-content-wrapper');
            return {
                sh: el ? el.scrollHeight : document.body.scrollHeight,
                ch: el ? el.clientHeight : window.innerHeight,
                dpr: window.devicePixelRatio || 1
            };
        }""")

        sh = metrics["sh"]
        ch = metrics["ch"]
        dpr = metrics["dpr"]
        print(f"    内容容器视口高度: {ch}px | 页面总滚动高度: {sh}px | 缩放比(DPR): {dpr}")

        slices = []
        cur_top = 0
        step = max(int(ch * 0.75), 300)

        # Slice 1 at top = 0
        img_bytes = wrapper_locator.screenshot()
        first_img = Image.open(io.BytesIO(img_bytes))
        slices.append((0, first_img))
        print(f"  [+] 截取第 1 屏切片 (Top: 0px, 分辨率: {first_img.size})")

        slice_index = 2
        while cur_top + ch < sh and slice_index <= 20:
            target_top = min(cur_top + step, sh - ch)
            page.evaluate(f"""(top) => {{
                const el = document.querySelector('.page-content-wrapper');
                if (el) el.scrollTop = top;
            }}""", target_top)
            time.sleep(0.7)  # 等待渲染与图表重绘

            actual_top = page.evaluate("""() => {
                const el = document.querySelector('.page-content-wrapper');
                return el ? el.scrollTop : 0;
            }""")

            delta = actual_top - cur_top
            if delta <= 0:
                break
            cur_top = actual_top

            shot_bytes = wrapper_locator.screenshot()
            slice_img = Image.open(io.BytesIO(shot_bytes))
            slices.append((delta, slice_img))
            print(f"  [+] 截取第 {slice_index} 屏切片 (下滚: {delta}px, 当前Top: {actual_top}px)")
            slice_index += 1

            if actual_top + ch >= sh - 5:
                break

        print(f"[*] 正在将 {len(slices)} 张局部屏图进行智能无缝拼接...")
        base_w = slices[0][1].width
        total_h = slices[0][1].height
        for delta, img in slices[1:]:
            total_h += int(round(delta * dpr))

        stitched_canvas = Image.new("RGB", (base_w, total_h), (255, 255, 255))
        stitched_canvas.paste(slices[0][1], (0, 0))
        curr_y = slices[0][1].height

        for delta, img in slices[1:]:
            shift = int(round(delta * dpr))
            crop_top = max(0, img.height - shift)
            cropped = img.crop((0, crop_top, img.width, img.height))
            stitched_canvas.paste(cropped, (0, curr_y))
            curr_y += cropped.height

        final_path.parent.mkdir(parents=True, exist_ok=True)
        stitched_canvas.save(str(final_path), quality=95)
        print(f"[✓] 微信式完整长截图已生成并无缝拼接完成！")
        print(f"    分辨率: {stitched_canvas.size[0]} x {stitched_canvas.size[1]} px")
        print(f"    保存路径: {final_path.resolve()}\n")

        # Also capture one full window view for context
        overview_path = final_path.parent / final_path.name.replace("_long.png", "_window.png")
        try:
            page.screenshot(path=str(overview_path))
            print(f"[✓] 视口全局对照图已保存至: {overview_path.resolve()}")
        except Exception:
            pass

        # Scroll back to top
        page.evaluate("const el = document.querySelector('.page-content-wrapper'); if (el) el.scrollTop = 0;")
        time.sleep(0.5)
        return str(final_path.resolve())

    def capture_snapshot(self, output_path: Optional[str] = None, wait_seconds: int = 8, headless: bool = False) -> str:
        """
        Launches Chrome with independent persistent profile (data/studio_chrome_profile),
        verifies 'All models' toggle, extracts 40+ model metrics via DOM,
        and captures a seamless stitched long screenshot down to page bottom.
        Supports both visible mode (for human calibration) and headless mode (for automated background calibration).
        """
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        chrome_exe = find_system_chrome()

        out_dir = SNAPSHOTS_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        filename = f"ai_studio_quota_{timestamp}_long.png"
        target_file = Path(output_path) if output_path else (out_dir / filename)

        mode_str = "无头后台静默探活模式 (Headless)" if headless else "前台独立可视模式 (Visible)"
        print(f"\n[*] 启动 Chrome ({mode_str} / 支持免登持久化):")
        print(f"    Profile 数据目录: {self.profile_dir}")
        print(f"    目标 URL:        {self.target_url}")
        print(f"    Chrome 核心:     {chrome_exe}")

        with sync_playwright() as p:
            # Launch persistent context directly to avoid port 9222 conflicts with everyday Chrome
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                executable_path=chrome_exe if os.path.exists(chrome_exe) else None,
                channel="chrome" if not os.path.exists(chrome_exe) else None,
                headless=headless,
                no_viewport=True,
                args=[
                    "--start-maximized",
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )

            page = context.pages[0] if context.pages else context.new_page()

            # Network RPC listener for quota payloads as background baseline
            network_quota_captured = []
            def _on_response(resp):
                try:
                    url = resp.url
                    if any(k in url.lower() for k in ["quota", "ratelimit", "project", "models"]):
                        if resp.status == 200 and "json" in resp.headers.get("content-type", ""):
                            data = resp.json()
                            network_quota_captured.append({"url": url, "data": data})
                except Exception:
                    pass

            page.on("response", _on_response)

            print(f"[*] 正在导航至: {self.target_url}")
            page.goto(self.target_url, wait_until="domcontentloaded", timeout=45000)

            # Check if login is needed
            def _is_on_ai_studio(p) -> bool:
                url = p.url
                title = p.title()
                if "accounts.google.com" in url or "signin" in url.lower():
                    return False
                if any(w in title for w in ["登录", "Sign in", "Sign-in", "Verify", "Google 账号"]):
                    return False
                return "aistudio.google.com" in url

            if not _is_on_ai_studio(page):
                print("\n" + "=" * 68)
                print(" ⚠️  [GOOGLE LOGIN REQUIRED / 请登录 Google 账号]")
                print("=" * 68)
                print(" 1. 请在弹出的 Chrome 窗口中输入邮箱、密码及手机/2FA 验证完成登录。")
                print(" 2. 登录成功后凭证将永久保存在 data/studio_chrome_profile 中，次次免登！")
                print(" 3. 系统会自动检测直到页面真正跳转回 AI Studio 配额看板。")
                print("=" * 68)

                login_success = False
                for elapsed in range(150):  # Wait up to 300 seconds (5 minutes)
                    time.sleep(2)
                    if _is_on_ai_studio(page):
                        print("\n[✓] 验证通过！检测到已成功进入 Google AI Studio 看板！继续执行...")
                        login_success = True
                        break
                    if elapsed % 10 == 0 and elapsed > 0:
                        print(f"[*] 等待用户完成登录验证中... (已等待 {elapsed*2} 秒 / 当前页面: 《{page.title()}》)")

                if not login_success:
                    print("\n[!] 登录等待超时（5分钟）。请重新运行并完成登录。")
                    context.close()
                    return ""

            print(f"[*] 页面标题: 《{page.title()}》")
            print(f"[*] 等待 {wait_seconds} 秒以供速率与配额指标完成初次渲染...")
            time.sleep(wait_seconds)

            # Step 1: Ensure 'All models' toggle is ON (without toggling off)
            self._ensure_all_models_toggle(page)

            # Step 2: Smooth scroll down to bottom once to trigger lazy load for all charts
            print("[*] 正在执行全量模型与图表预热平滑滚动...")
            for _ in range(4):
                page.evaluate("window.scrollBy({top: 600, behavior: 'smooth'})")
                time.sleep(0.6)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(1.0)

            # Step 3: Extract accurate DOM structured data for all models
            models_data = self._extract_dynamic_dom_elements(page)

            # Step 4: Save structured JSON data
            json_file = out_dir / f"ai_studio_quota_{timestamp}.json"
            try:
                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump({
                        "timestamp": timestamp,
                        "url": self.target_url,
                        "model_count": len(models_data),
                        "models": models_data,
                    }, f, ensure_ascii=False, indent=2)
                print(f"[✓] 40+ 模型结构化配额指标已保存至: {json_file.resolve()}")
            except Exception as e:
                print(f"[!] 保存 JSON 失败: {e}")

            # Step 5: Save network response JSON if any
            if network_quota_captured:
                net_file = out_dir / f"ai_studio_network_rpc_{timestamp}.json"
                try:
                    with open(net_file, "w", encoding="utf-8") as f:
                        json.dump(network_quota_captured, f, ensure_ascii=False, indent=2)
                    print(f"[✓] 捕获到的后台 Network Quota RPC 数据已保存至: {net_file.resolve()}")
                except Exception:
                    pass

            # Step 6: WeChat-style seamless stitched long screenshot
            saved_path = self._capture_stitched_long_screenshot(page, target_file)

            # Keep window open for a brief moment
            time.sleep(2)
            context.close()
            return saved_path
