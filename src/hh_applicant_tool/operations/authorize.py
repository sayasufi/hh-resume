from __future__ import annotations

import argparse
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

try:
    from playwright.async_api import async_playwright
except ImportError:
    pass

from ..main import BaseOperation
from ..utils.terminal import print_kitty_image, print_sixel_mage

if TYPE_CHECKING:
    from ..main import HHApplicantTool


HH_ANDROID_SCHEME = "hhandroid"

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor()


async def ainput(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, input, prompt)


class Operation(BaseOperation):
    """Авторизация через Playwright"""

    __aliases__: list = ["authenticate", "auth", "login"]

    # Селекторы
    SELECT_LOGIN_INPUT = 'input[data-qa="login-input-username"]'
    SELECT_EXPAND_PASSWORD = 'button[data-qa="expand-login-by_password"]'
    SELECT_PASSWORD_INPUT = 'input[data-qa="login-input-password"]'
    SELECT_CODE_CONTAINER = 'div[data-qa="account-login-code-input"]'
    SELECT_PIN_CODE_INPUT = 'input[data-qa="magritte-pincode-input-field"]'
    SELECT_CAPTCHA_IMAGE = 'img[data-qa="account-captcha-picture"]'
    SELECT_CAPTCHA_INPUT = 'input[data-qa="account-captcha-input"]'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._args = None

    @property
    def is_headless(self) -> bool:
        """Свойство, определяющее режим работы браузера"""
        return not self._args.no_headless and self.is_automated

    @property
    def is_automated(self) -> bool:
        return not self._args.manual

    @property
    def selector_timeout(self) -> int | None:
        """Вспомогательное свойство для таймаутов: None если headless, иначе 500мс"""
        return None if self.is_headless else 5000

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "username",
            nargs="?",
            help="Email или телефон",
        )
        parser.add_argument(
            "--password",
            "-p",
            help="Пароль для входа (если не указать, то вход будет по одноразовому коду)",
        )
        parser.add_argument(
            "--no-headless",
            "-n",
            action="store_true",
            help="Показать окно браузера для отладки (отключает headless режим).",
        )
        parser.add_argument(
            "-m",
            "--manual",
            action="store_true",
            help="Ручной режим ввода кредов, редирект будет перехвачен.",
        )
        parser.add_argument(
            "-k",
            "--use-kitty",
            "--kitty",
            action="store_true",
            help="Использовать kitty protocol для вывода капчи в терминал.",
        )
        parser.add_argument(
            "-s",
            "--use-sixel",
            "--sixel",
            action="store_true",
            help="Использовать sixel protocol для вывода капчи в терминал.",
        )

    async def run(self, tool: HHApplicantTool) -> None:
        self._args = tool.args
        try:
            await self._main(tool)
        except (KeyboardInterrupt, asyncio.TimeoutError):
            logger.warning("Что-то пошло не так")
            return 1

    async def _main(self, tool: HHApplicantTool) -> None:
        args = tool.args
        api_client = tool.api_client
        storage = tool.storage

        if self.is_automated:
            username = (
                args.username
                or await storage.settings.get_value("auth.username")
                or (await ainput("👤 Введите email или телефон: "))
            ).strip()

            if not username:
                raise RuntimeError("Empty username")

            logger.debug(f"authenticate with: {username}")

        proxies = api_client.proxies
        proxy_url = proxies.get("https")

        chromium_args: list[str] = []
        if proxy_url:
            chromium_args.append(f"--proxy-server={proxy_url}")
            logger.debug(f"Используется прокси: {proxy_url}")

        if self.is_headless:
            logger.debug("Headless режим активен")

        async with async_playwright() as pw:
            logger.debug("Запуск браузера...")

            browser = await pw.chromium.launch(
                headless=self.is_headless,
                args=chromium_args,
            )

            try:
                # https://github.com/microsoft/playwright/blob/main/packages/playwright-core/src/server/deviceDescriptorsSource.json
                android_device = pw.devices["Galaxy A55"]
                context = await browser.new_context(**android_device)
                page = await context.new_page()

                # async def route_handler(route):
                #      req = route.request
                #      url = req.url.lower()

                #      # Блокировка сканирования локальных портов
                #      if any(d in url for d in ["localhost", "127.0.0.1", "::1"]):
                #           logger.debug(f"🛑  Блокировка запроса на локальный порт: {url}")
                #           return await route.abort()

                #      # Оптимизация трафика в headless
                #      if is_headless and req.resource_type in [
                #           "image",
                #           "stylesheet",
                #           "font",
                #           "media",
                #      ]:
                #           return await route.abort()

                #      await route.continue_()

                # почему-то добавление этого обработчика вешает все
                # await page.route("**/*", route_handler)

                code_future: asyncio.Future[str | None] = asyncio.Future()

                def handle_request(request):
                    url = request.url
                    if url.startswith(f"{HH_ANDROID_SCHEME}://"):
                        logger.info(f"Перехвачен OAuth redirect: {url}")
                        if not code_future.done():
                            sp = urlsplit(url)
                            code = parse_qs(sp.query).get("code", [None])[0]
                            code_future.set_result(code)

                page.on("request", handle_request)

                logger.debug(
                    f"Переход на страницу OAuth: {api_client.oauth_client.authorize_url}"
                )
                await page.goto(
                    api_client.oauth_client.authorize_url,
                    timeout=30000,
                    wait_until="load",
                )

                if self.is_automated:
                    logger.debug(
                        f"Ожидание поля логина {self.SELECT_LOGIN_INPUT}"
                    )
                    await page.wait_for_selector(
                        self.SELECT_LOGIN_INPUT, timeout=self.selector_timeout
                    )
                    await page.fill(self.SELECT_LOGIN_INPUT, username)
                    logger.debug("Логин введен")

                    password = args.password or await storage.settings.get_value(
                        "auth.password"
                    )
                    if password:
                        await self._direct_login(page, password)
                    else:
                        await self._onetime_code_login(page)

                logger.debug("Ожидание OAuth-кода...")

                auth_code = await asyncio.wait_for(
                    code_future, timeout=[None, 30.0][self.is_automated]
                )

                page.remove_listener("request", handle_request)

                logger.debug("Код получен, пробуем получить токен...")
                token = await api_client.oauth_client.authenticate(auth_code)
                api_client.handle_access_token(token)

                print("🔓 Авторизация прошла успешно!")

                # Сохраняем логин и пароль
                if self.is_automated:
                    await storage.settings.set_value("auth.username", username)
                    if args.password:
                        await storage.settings.set_value(
                            "auth.password", args.password
                        )

                await storage.settings.set_value(
                    "auth.last_login", datetime.now()
                )

                # storage.settings.set_value(
                #     "auth.access_token", token["access_token"]
                # )
                # storage.settings.set_value(
                #     "auth.refresh_token", token["refresh_token"]
                # )
                # storage.settings.set_value(
                #     "auth.refresh_token", token["expires_in"]
                # )

            finally:
                logger.debug("Закрытие браузера")
                await browser.close()

    async def _direct_login(self, page, password: str) -> None:
        logger.info("Вход по паролю...")

        logger.debug(
            f"Клик по кнопке развертывания пароля: {self.SELECT_EXPAND_PASSWORD}"
        )
        await page.click(self.SELECT_EXPAND_PASSWORD)

        await self._handle_captcha(page)

        logger.debug(f"Ожидание поля пароля: {self.SELECT_PASSWORD_INPUT}")
        await page.wait_for_selector(
            self.SELECT_PASSWORD_INPUT, timeout=self.selector_timeout
        )
        await page.fill(self.SELECT_PASSWORD_INPUT, password)
        await page.press(self.SELECT_PASSWORD_INPUT, "Enter")
        logger.debug("Форма с паролем отправлена")

    async def _onetime_code_login(self, page) -> None:
        logger.info("Вход по одноразовому коду...")

        await page.press(self.SELECT_LOGIN_INPUT, "Enter")

        await self._handle_captcha(page)

        logger.debug(
            f"Ожидание контейнера ввода кода: {self.SELECT_CODE_CONTAINER}"
        )

        await page.wait_for_selector(
            self.SELECT_CODE_CONTAINER, timeout=self.selector_timeout
        )

        print("📨 Код был отправлен. Проверьте почту или SMS.")
        code = (await ainput("📩 Введите полученный код: ")).strip()

        if not code:
            raise RuntimeError("Код подтверждения не может быть пустым.")

        logger.debug(f"Ввод кода в {self.SELECT_PIN_CODE_INPUT}")
        await page.fill(self.SELECT_PIN_CODE_INPUT, code)
        await page.press(self.SELECT_PIN_CODE_INPUT, "Enter")
        logger.debug("Форма с кодом отправлена")

    async def _handle_captcha(self, page):
        try:
            captcha_element = await page.wait_for_selector(
                self.SELECT_CAPTCHA_IMAGE,
                timeout=self.selector_timeout,
                state="visible",
            )
        except Exception:
            logger.debug("Капчи нет, продолжаем как обычно.")
            return

        if not (self._args.use_kitty or self._args.use_sixel):
            raise RuntimeError(
                "Требуется ввод капчи!",
            )

        # box = await captcha_element.bounding_box()

        # width = int(box["width"])
        # height = int(box["height"])

        img_bytes = await captcha_element.screenshot()

        print(
            "Если вы не видите картинку ниже, то ваш терминал не поддерживает"
            " вывод изображений."
        )
        print()

        if self._args.use_kitty:
            print_kitty_image(img_bytes)

        if self._args.use_sixel:
            print_sixel_mage(img_bytes)

        captcha_text = (await ainput("Введите текст с картинки: ")).strip()

        await page.fill(self.SELECT_CAPTCHA_INPUT, captcha_text)
        await page.press(self.SELECT_CAPTCHA_INPUT, "Enter")
