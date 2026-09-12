"""浏览器环境管理器（M4：RPA/UI 操作评测）。

基于 Playwright，提供 headless Chromium 浏览器的启动、导航、元素检查、截图等能力。
用于 UI checkpoint 验证（ui_element_exists / browser_url_contains / http_status / screenshot_matches）。

设计原则：
- 懒加载：只有在需要 UI checkpoint 时才启动浏览器
- 单例：一次评测运行中共享一个浏览器实例，避免重复启动
- 自动清理：评测结束后自动关闭浏览器
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agent_eval.log import get_logger

logger = get_logger(__name__)


@dataclass
class BrowserState:
    """浏览器状态（用于 checkpoint 验证）。"""
    url: str = ""
    title: str = ""
    status_code: Optional[int] = None


class BrowserEnvironment:
    """Playwright 浏览器环境管理器。

    用法：
        env = BrowserEnvironment()
        with env.session() as page:
            page.goto("https://example.com")
            assert env.element_exists(page, "h1")
    """

    def __init__(self, headless: bool = True, timeout: float = 30.0):
        self.headless = headless
        self.timeout = timeout * 1000  # Playwright 用毫秒
        self._playwright = None
        self._browser = None
        self._context = None

    def start(self) -> None:
        """启动 Playwright 和浏览器。"""
        from playwright.sync_api import sync_playwright

        if self._browser is not None:
            return
        logger.info("启动 Playwright 浏览器（headless=%s）", self.headless)
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="ai-agent-eval/1.0 (RPA-UI-Evaluation)",
        )
        self._context.set_default_timeout(self.timeout)

    def close(self) -> None:
        """关闭浏览器和 Playwright。"""
        if self._context:
            self._context.close()
            self._context = None
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        logger.info("Playwright 浏览器已关闭")

    @contextmanager
    def session(self):
        """创建一个浏览器页面会话（自动关闭页面）。"""
        if self._browser is None:
            self.start()
        page = self._context.new_page()
        try:
            yield page
        finally:
            page.close()

    def navigate(self, url: str) -> BrowserState:
        """导航到 URL，返回浏览器状态。"""
        with self.session() as page:
            response = page.goto(url, wait_until="domcontentloaded")
            state = BrowserState(
                url=page.url,
                title=page.title(),
                status_code=response.status if response else None,
            )
            logger.info("导航到 %s → %s (status=%s)", url, state.url, state.status_code)
            return state

    def element_exists(self, url: str, selector: str) -> tuple[bool, str]:
        """检查 URL 页面中是否存在 CSS 选择器匹配的元素。

        Returns:
            (exists, detail)
        """
        try:
            with self.session() as page:
                page.goto(url, wait_until="domcontentloaded")
                # 等待元素出现（最多 timeout 秒）
                try:
                    page.wait_for_selector(selector, timeout=self.timeout)
                    count = page.locator(selector).count()
                    return True, f"元素存在（{count} 个匹配: {selector}）"
                except Exception:
                    return False, f"元素不存在: {selector}"
        except Exception as e:
            return False, f"页面访问失败: {e}"

    def url_contains(self, start_url: str, pattern: str) -> tuple[bool, str]:
        """导航到 start_url，检查最终 URL 是否包含 pattern。

        用于验证页面跳转（如登录后跳转到 dashboard）。
        """
        try:
            with self.session() as page:
                page.goto(start_url, wait_until="domcontentloaded")
                # 等待可能的重定向
                page.wait_for_timeout(1000)
                current_url = page.url
                if pattern in current_url:
                    return True, f"URL 包含 '{pattern}': {current_url}"
                return False, f"URL 不包含 '{pattern}': {current_url}"
        except Exception as e:
            return False, f"页面访问失败: {e}"

    def http_status(self, url: str, expected_pattern: str = "2") -> tuple[bool, str]:
        """检查 URL 的 HTTP 状态码是否匹配 expected_pattern。

        expected_pattern 支持：
        - 精确状态码："200"
        - 前缀匹配："2"（匹配 2xx）、"3"（匹配 3xx）
        - 范围："200-299"
        """
        try:
            with self.session() as page:
                response = page.goto(url, wait_until="domcontentloaded")
                status = response.status if response else 0
                # 解析期望模式
                if "-" in expected_pattern:
                    low, high = expected_pattern.split("-", 1)
                    matched = int(low) <= status <= int(high)
                    detail = f"状态码 {status} 在范围 {expected_pattern} 内" if matched else f"状态码 {status} 不在范围 {expected_pattern} 内"
                else:
                    matched = str(status).startswith(expected_pattern)
                    detail = f"状态码 {status} 匹配 '{expected_pattern}'" if matched else f"状态码 {status} 不匹配 '{expected_pattern}'"
                return matched, detail
        except Exception as e:
            return False, f"页面访问失败: {e}"

    def screenshot(self, url: str, output_path: Path, full_page: bool = False) -> Optional[Path]:
        """截取 URL 页面的截图。"""
        try:
            with self.session() as page:
                page.goto(url, wait_until="networkidle")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output_path), full_page=full_page)
                logger.info("截图已保存: %s", output_path)
                return output_path
        except Exception as e:
            logger.warning("截图失败: %s", e)
            return None


# 全局浏览器环境单例（一次评测运行中共享）
_browser_env: Optional[BrowserEnvironment] = None


def get_browser_env() -> BrowserEnvironment:
    """获取全局浏览器环境单例。"""
    global _browser_env
    if _browser_env is None:
        _browser_env = BrowserEnvironment()
    return _browser_env


def close_browser_env() -> None:
    """关闭全局浏览器环境。"""
    global _browser_env
    if _browser_env is not None:
        _browser_env.close()
        _browser_env = None
