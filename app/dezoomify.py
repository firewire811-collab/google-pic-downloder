from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEZOOMIFY_URL = "https://dezoomify.ophir.dev/"


class DezoomifyPageError(Exception):
    """dezoomify 페이지에서 'Oops, we have a problem' 같은 인페이지 오류가 발생했을 때."""


@dataclass(frozen=True)
class DezoomifyResult:
    saved_path: Path


def download_via_dezoomify(
    asset_url: str,
    dest_path: Path,
    *,
    locale: str = "ko-KR",
    timezone_id: str = "Asia/Seoul",
    headless: bool = False,
    timeout_s: float = 6 * 60,
    temp_downloads_dir: Path | None = None,
) -> DezoomifyResult:
    """Download an image via Dezoomify and save it to dest_path.

    This opens a separate (headed) Chromium window by default so the user can see
    the process. It uses browser-context emulation for Korean locale.
    """

    timeout_ms = int(timeout_s * 1000)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if temp_downloads_dir is not None:
        temp_downloads_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            downloads_path=str(temp_downloads_dir) if temp_downloads_dir else None,
        )
        try:
            context = browser.new_context(
                locale=locale,
                timezone_id=timezone_id,
                accept_downloads=True,
                extra_http_headers={
                    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )
            page = context.new_page()

            page.goto(DEZOOMIFY_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            page.locator("#url").fill(asset_url)
            # Dezoomify starts via a form submit button.
            page.get_by_role("button", name="Dezoomify !").click(timeout=timeout_ms)

            # 성공(save link 등장) 또는 실패("Oops" 에러) 중 먼저 오는 쪽을 기다린다.
            # JavaScript 함수가 'ok' | 'error' | null 을 반환한다.
            result = page.evaluate_handle(
                """() => new Promise((resolve) => {
                    const check = () => {
                        const link = document.querySelector('#status a[download="dezoomify-result.jpg"]');
                        if (link) return resolve('ok');
                        const el = document.querySelector('#status');
                        if (el && /oops,?\\s+we have a problem/i.test(el.innerText || ''))
                            return resolve('error');
                        setTimeout(check, 500);
                    };
                    check();
                })""",
            )
            outcome = result.json_value()

            if outcome == "error":
                # "Oops..." 이후 텍스트만 추출하여 에러 메시지로 사용
                try:
                    full_text = page.locator("#status").inner_text(timeout=3_000)
                    m = re.search(r"(oops.*)", full_text, re.IGNORECASE | re.DOTALL)
                    error_msg = m.group(1).strip()[:400] if m else full_text.strip()[:400]
                except Exception:
                    error_msg = "Oops, we have a problem (상세 내용 없음)"
                raise DezoomifyPageError(error_msg)

            save_link = page.locator("#status a[download='dezoomify-result.jpg']")
            save_link.wait_for(state="visible", timeout=timeout_ms)

            # Wait until the link is ready (text changes + href becomes a blob URL).
            try:
                handle = save_link.element_handle(timeout=timeout_ms)
                page.wait_for_function(
                    "(el) => (el.textContent || '').includes('Save image') && el.getAttribute('href') && el.getAttribute('href') !== '#'",
                    arg=handle,
                    timeout=timeout_ms,
                )
            except PlaywrightTimeoutError:
                # Some dezoomers can be slow; if the link exists but didn't flip yet,
                # still attempt the click and let expect_download time out.
                pass

            with page.expect_download(timeout=timeout_ms) as dl_info:
                save_link.click(timeout=timeout_ms)
            download = dl_info.value
            download.save_as(dest_path)
            # Windows 파일 시스템 동기화 대기 (이후 이미지 리사이즈 안정화)
            time.sleep(2)
            return DezoomifyResult(saved_path=dest_path)
        finally:
            browser.close()
