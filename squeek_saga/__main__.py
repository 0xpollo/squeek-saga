"""Entry point: one process runs the saga worker, the website and the Discord bot."""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx
import uvicorn

from .chain import ChainReader
from .config import load_settings
from .db import Store
from .deployment import DeploymentInvalid, DeploymentUnavailable, load_abi, load_deployment
from .engine import SagaEngine
from .squeek_api import SqueekApi
from .story import Storyteller
from .web import create_app

log = logging.getLogger("squeek_saga")


async def amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(follow_redirects=False) as http:
        try:
            deployment = await load_deployment(settings.data_dir, http)
            core_abi = await load_abi(settings.data_dir, http, "SqueekCore")
        except (DeploymentUnavailable, DeploymentInvalid) as exc:
            # Addresses and ABIs come only from the published record. Without it we stop.
            log.error("Cannot start: %s", exc)
            return 2
        if deployment.from_cache:
            log.warning("docs.squeekchain.com unreachable; using the last deployment record fetched from it")
        log.info("deployment %s on chain %s, SqueekCore %s", deployment.release, deployment.chain_id,
                 deployment.core_address)

        store = Store(settings.db_path)
        reader = ChainReader(settings.rpc_url, deployment, core_abi)
        storyteller = Storyteller.from_environment(settings.saga_model, settings.anthropic_configured)
        if not settings.anthropic_configured:
            log.warning("ANTHROPIC_API_KEY is not set: chapters will use the built-in stock text")
        engine = SagaEngine(settings=settings, deployment=deployment, store=store, reader=reader,
                            storyteller=storyteller)
        api = SqueekApi(http)

        server = uvicorn.Server(
            uvicorn.Config(create_app(engine, api), host="0.0.0.0", port=settings.port, log_level="warning",
                           proxy_headers=True, forwarded_allow_ips="*")
        )
        tasks = [
            asyncio.create_task(engine.run_forever(), name="saga-worker"),
            asyncio.create_task(server.serve(), name="web"),
        ]
        bot = None
        if settings.discord_token:
            from .bot import SagaBot

            bot = SagaBot(engine, api)
            tasks.append(asyncio.create_task(bot.start(settings.discord_token), name="discord"))
        else:
            log.warning("DISCORD_TOKEN is not set: running the website and worker without the bot")
        log.info("Squeek Saga up: site on port %s", settings.port)

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception():
                log.error("task %s stopped: %r", task.get_name(), task.exception())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if bot is not None and not bot.is_closed():
            await bot.close()
        await reader.close()
        store.close()
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
