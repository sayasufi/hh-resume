"""Resolve HH image challenges using the configured 2captcha account."""
import asyncio
import base64
import logging
import time
from urllib.parse import unquote, urlsplit

import httpx

logger = logging.getLogger(__name__)
PICTURE = '[data-qa="account-captcha-picture"]'
INPUT = '[data-qa="account-captcha-input"]'
SUBMIT = '[data-qa="account-captcha-submit"]'


def valid_captcha_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "hh.ru"
                and parsed.path == "/account/captcha" and not parsed.username
                and parsed.port in (None, 443))
    except ValueError:
        return False


async def solve_image(client, key: str, image: bytes) -> str:
    if not image or len(image) > 100_000:
        raise ValueError("Invalid captcha image size")
    response = await client.post("https://api.2captcha.com/createTask", json={
        "clientKey": key, "languagePool": "rn",
        "task": {"type": "ImageToTextTask", "body": base64.b64encode(image).decode()},
    })
    response.raise_for_status()
    task = response.json()
    if task.get("errorId") or not task.get("taskId"):
        raise ValueError(f"2captcha: {task.get('errorCode', 'invalid task response')}")
    for _ in range(24):
        await asyncio.sleep(5)
        response = await client.post("https://api.2captcha.com/getTaskResult", json={
            "clientKey": key, "taskId": task["taskId"],
        })
        response.raise_for_status()
        result = response.json()
        if result.get("errorId"):
            raise ValueError(f"2captcha: {result.get('errorCode', 'solver error')}")
        if result.get("status") == "ready":
            text = (result.get("solution") or {}).get("text", "").strip()
            if not text:
                raise ValueError("2captcha returned an empty answer")
            return text
    raise TimeoutError("2captcha image task timed out")


async def _solve_in_browser(url: str, key: str, user_agent: str | None, proxy: str | None) -> bool:
    from playwright.async_api import async_playwright

    launch = {"headless": True}
    if proxy:
        parsed = urlsplit(proxy)
        launch["proxy"] = {"server": f"{parsed.scheme}://{parsed.netloc.rsplit('@', 1)[-1]}"}
        if parsed.username:
            launch["proxy"]["username"] = unquote(parsed.username)
            launch["proxy"]["password"] = unquote(parsed.password or "")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch)
        try:
            context = await browser.new_context(**({"user_agent": user_agent} if user_agent else {}))
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            # Visibility alone is insufficient: a screenshot of an unloaded image
            # is accepted by the solver but cannot be recognized.
            await page.wait_for_function(
                """selector => {const image=document.querySelector(selector);
                return image && image.complete && image.naturalWidth > 0;}""",
                arg=PICTURE, timeout=15_000,
            )
            image = await page.locator(PICTURE).screenshot()
            async with httpx.AsyncClient(timeout=20) as client:
                answer = await solve_image(client, key, image)
            await page.locator(INPUT).fill(answer)
            await page.locator(SUBMIT).click()
            await page.wait_for_url(
                lambda current: urlsplit(current).path != "/account/captcha", timeout=15_000,
            )
            return await page.locator(INPUT).count() == 0
        finally:
            await browser.close()


async def solve_captcha(url: str, user_agent: str | None = None, proxy: str | None = None) -> bool:
    """One paid attempt per account per two minutes, under an advisory lock."""
    if not url or not valid_captcha_url(url):
        return False
    from ..storage import pgconn

    connection = None
    try:
        key = await asyncio.to_thread(pgconn.get_setting, "habr.2captcha_key", None, "_global")
        if not key:
            return False
        account = pgconn.get_account()
        connection = await asyncio.to_thread(pgconn.connect)
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"hh:captcha:{account}",))
            if not cursor.fetchone()[0]:
                return False
        last_attempt = await asyncio.to_thread(pgconn.get_setting, "hh.captcha.last_attempt", 0)
        if time.time() - float(last_attempt) < 120:
            return False
        await asyncio.to_thread(pgconn.set_setting, "hh.captcha.last_attempt", time.time())
        logger.info("HH captcha: resolving image via 2captcha")
        solved = await asyncio.wait_for(_solve_in_browser(url, key, user_agent, proxy), timeout=180)
        if solved:
            logger.info("HH captcha accepted; retrying the blocked request")
        return solved
    except Exception as error:
        logger.warning("HH captcha could not be resolved (%s)", type(error).__name__)
        return False
    finally:
        if connection is not None:
            await asyncio.to_thread(connection.close)
