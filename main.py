import logging
import os
from datetime import datetime

import aiohttp
import discord
import dns.asyncresolver
import dns.exception
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

intents = discord.Intents.default()

class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        logger.opt(exception=record.exc_info).log(level, record.getMessage())


class MyBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.dns_check_hosts = {
            "外部": os.getenv("DNS_CHECK_EXTERNAL_HOST", os.getenv("DNS_CHECK_HOST", "")).strip(),
            "内部": os.getenv("DNS_CHECK_INTERNAL_HOST", "").strip(),
        }
        self.dns_host_setting_names = {
            "外部": "DNS_CHECK_EXTERNAL_HOST (or legacy DNS_CHECK_HOST)",
            "内部": "DNS_CHECK_INTERNAL_HOST",
        }
        self.dns_server = os.getenv("DNS_SERVER", "").strip()
        self.dns_resolver = dns.asyncresolver.Resolver(configure=False)
        self.dns_results: dict[str, tuple[str, ...] | None] = {}
        self.http_check_urls = [
            url.strip() for url in os.getenv("HTTP_CHECK_URLS", "").split(",") if url.strip()
        ]
        self.http_results: dict[str, str] = {}
        self.http_session: aiohttp.ClientSession | None = None
        self.dns_alert_channel_id: int | None = None

    async def close(self):
        if self.http_session is not None:
            await self.http_session.close()
        await super().close()

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Slash commands synced.")
        self.change_status.start()
        channel_id = os.getenv("DNS_ALERT_CHANNEL_ID", "")
        channel_valid = True
        if not channel_id:
            logger.error("DNS_ALERT_CHANNEL_ID is required; monitoring will not start.")
            channel_valid = False
        else:
            try:
                self.dns_alert_channel_id = int(channel_id)
            except ValueError:
                logger.error("DNS_ALERT_CHANNEL_ID must be a valid channel ID.")
                channel_valid = False

        configuration_valid = channel_valid
        for label, host in self.dns_check_hosts.items():
            if not host:
                logger.error(
                    f"{self.dns_host_setting_names[label]} is required; "
                    "DNS monitoring will not start."
                )
                configuration_valid = False

        if not self.dns_server:
            logger.error("DNS_SERVER is required; DNS monitoring will not start.")
            configuration_valid = False
        else:
            try:
                self.dns_resolver.nameservers = [self.dns_server]
            except ValueError:
                logger.error("DNS_SERVER must be a valid DNS server IP address.")
                configuration_valid = False

        if configuration_valid:
            self.check_dns.start()

        if not self.http_check_urls:
            logger.info("HTTP_CHECK_URLS is empty; HTTP monitoring will not start.")
        elif channel_valid:
            self.http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
            self.check_http.start()

    async def on_ready(self):
        logger.info(f"Logged in as {self.user}")

    @tasks.loop(seconds=30)
    async def change_status(self):
        await self.change_presence(status=discord.Status.online, activity=discord.CustomActivity(name=f'Alive at {datetime.now().strftime("%m/%d %H:%M")}'))
        logger.debug("status updated")

    @change_status.before_loop
    async def before_change_status(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def check_dns(self):
        for label, host in self.dns_check_hosts.items():
            error: Exception | None = None
            try:
                answer = await self.dns_resolver.resolve(
                    host,
                    "A",
                    lifetime=10,
                    search=False,
                )
                result = tuple(sorted({record.address for record in answer}))
                logger.debug(f"DNS query succeeded for {host} via {self.dns_server}: {result}")
            except (dns.exception.DNSException, OSError, ValueError) as query_error:
                result = None
                error = query_error
                logger.error(
                    f"DNS check failed for {host} via {self.dns_server}: {query_error}"
                )

            if label not in self.dns_results:
                self.dns_results[label] = result
                logger.info(f"Initial DNS result recorded for {label} domain {host}: {result}")
                continue

            previous_result = self.dns_results[label]
            if previous_result != result:
                self.dns_results[label] = result
                await self.send_dns_alert(label, host, previous_result, result, error)

    @check_dns.before_loop
    async def before_check_dns(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def check_http(self):
        for url in self.http_check_urls:
            error: Exception | None = None
            try:
                assert self.http_session is not None
                async with self.http_session.get(url, allow_redirects=False) as response:
                    status = response.status
                result = f"OK ({status})" if 200 <= status <= 204 else f"NG ({status})"
            except (aiohttp.ClientError, TimeoutError) as request_error:
                error = request_error
                result = f"NG ({type(request_error).__name__})"
            logger.debug(f"HTTP check for {url}: {result}")

            if url not in self.http_results:
                self.http_results[url] = result
                logger.info(f"Initial HTTP result recorded for {url}: {result}")
                continue

            previous_result = self.http_results[url]
            if previous_result != result:
                self.http_results[url] = result
                error_text = f"\n```{type(error).__name__}: {error}```" if error else ""
                await self.send_alert(
                    f"🔄 HTTPチェック結果が変化しました: `{url}`\n"
                    f"前回: `{previous_result}`\n今回: `{result}`{error_text}"
                )

    @check_http.before_loop
    async def before_check_http(self):
        await self.wait_until_ready()

    async def send_dns_alert(
        self,
        label: str,
        host: str,
        previous_result: tuple[str, ...] | None,
        result: tuple[str, ...] | None,
        error: Exception | None,
    ) -> None:
        previous_text = ", ".join(previous_result) if previous_result else "失敗"
        result_text = ", ".join(result) if result else "失敗"
        error_text = f"\n```{type(error).__name__}: {error}```" if error else ""
        await self.send_alert(
            f"🔄 {label}ドメインのDNS結果が変化しました: `{host}` "
            f"(DNSサーバー: `{self.dns_server}`)\n"
            f"前回: `{previous_text}`\n今回: `{result_text}`{error_text}"
        )

    async def send_alert(self, message: str) -> None:
        if self.dns_alert_channel_id is None:
            return

        try:
            channel = self.get_channel(self.dns_alert_channel_id)
            if channel is None:
                channel = await self.fetch_channel(self.dns_alert_channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                raise TypeError("Configured channel does not support messages.")
            await channel.send(message)
        except (discord.HTTPException, TypeError) as send_error:
            logger.exception(f"Failed to send alert: {send_error}")


client = MyBot()
discord.utils.setup_logging(handler=InterceptHandler())
client.run(os.getenv("API_KEY", ""), log_handler=None)