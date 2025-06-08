#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Telegram bot that returns results from various ML models.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see [http://www.gnu.org/licenses/].

import asyncio
import base64
import contextlib
import io
import json
import logging
import re
import signal
import sys
import traceback
from typing import Optional

import banana_dev
import httpx
from ml_telegram_bot.connections import Connections
from ml_telegram_bot.llm_agent import LlmAgent
from ml_telegram_bot.storage import Storage
import replicate
from telegram import InputMediaPhoto
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder
from telegram.ext import CallbackContext
from telegram.ext import CommandHandler
from telegram.ext import ContextTypes
from telegram.ext import filters
from telegram.ext import MessageHandler
from telegram.helpers import escape_markdown
import yaml

try:
    from yaml import CSafeLoader as Loader
except ImportError:
    from yaml import SafeLoader


class Bot:
    logger = logging.getLogger(__name__)
    chat_ids = []
    replicate = None
    application = None
    config = None
    shutdown_done = False

    async def stablediffusion(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_chat_id = update.message.chat.id
        if self.chat_ids != [] and bot_chat_id not in self.chat_ids:
            self.logger.warning("Unauthorized access from %d detected !" % bot_chat_id)
            self.logger.warning(update)
            return
        typing_msg = update.message.reply_chat_action(ChatAction.UPLOAD_PHOTO)
        prompt, options = self.parse_prompt(" ".join(context.args))
        self.logger.info(
            "Running SDXL with prompt '%s' and options %s", prompt, options
        )
        model = self.replicate.models.get("stability-ai/sdxl")
        version = model.versions.get(
            "d830ba5dabf8090ec0db6c10fc862c6eb1c929e1a194a5411852d25fd954ac82"
        )

        # https://replicate.com/stability-ai/sdxl/versions/d830ba5dabf8090ec0db6c10fc862c6eb1c929e1a194a5411852d25fd954ac82
        inputs = {
            "prompt": prompt,
        }
        inputs.update(self.config["replicate"]["models"]["stablediffusion"])
        if "negative_prompt" in options:
            inputs["negative_prompt"] = options["negative_prompt"]
        if "size" in options:
            width, height = tuple(int(v) for v in options["size"].split("x"))
            inputs["width"] = width
            inputs["height"] = height

        await typing_msg
        self.logger.info("Final inputs to model: %s", inputs)

        output = version.predict(**inputs)
        self.logger.info("Outcome: %s", json.dumps(output))

        if len(output) == 1:
            img = await self.download_image(output[0])
            await update.message.reply_photo(img, caption=prompt[0:1000])
        elif len(output) > 1:
            await update.message.reply_media_group(
                [InputMediaPhoto(url) for url in output], caption=prompt[0:1000]
            )
        else:
            await typing_msg
            raise Exception("Didn't get any output, that's weird")

    async def dreamshaper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_chat_id = update.message.chat.id
        if self.chat_ids != [] and bot_chat_id not in self.chat_ids:
            self.logger.warning("Unauthorized access from %d detected !" % bot_chat_id)
            self.logger.warning(update)
            return
        typing_msg = update.message.reply_chat_action(ChatAction.UPLOAD_PHOTO)
        prompt, options = self.parse_prompt(" ".join(context.args))
        self.logger.info(
            "Running DreamShaper with prompt '%s' and options %s", prompt, options
        )

        model = banana_dev.Client(
            api_key=self.config["banana"]["token"],
            url=self.config["banana"]["models"]["dreamshaper"]["url"],
        )

        inputs = {
            "prompt": prompt,
        }
        inputs.update(self.config["banana"]["models"]["dreamshaper"])
        if "negative_prompt" in options:
            inputs["negative_prompt"] = options["negative_prompt"]

        await typing_msg
        self.logger.info("Final inputs to model: %s", inputs)
        output, meta = model.call("/", inputs)
        self.logger.info("Outcome: %s", json.dumps(output))

        await update.message.reply_photo(
            base64.b64decode(output["output"]), caption=prompt
        )

    async def editphoto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_chat_id = update.message.chat.id
        if self.chat_ids != [] and bot_chat_id not in self.chat_ids:
            self.logger.warning("Unauthorized access from %d detected !", bot_chat_id)
            self.logger.warning(update)
            return
        typing_msg = update.message.reply_chat_action(ChatAction.UPLOAD_PHOTO)

        msg = re.sub(r"^[Ee]dit:\s*", "", update.message.caption)
        prompt, options = self.parse_prompt(msg)

        model_name = options.get("model", "timothybrooks/pix2pix-instruct").strip()
        del options['model']

        best_image = max(
            # Not too big
            [ph for ph in update.message.photo if ph.width * ph.height <= 1024 * 1024],
            # Biggest size that isn't too big
            key=lambda ps: ps.width * ps.height,
        )

        self.logger.info("Downloading image for editing")

        image_file = await best_image.get_file()

        image_bytes = await image_file.download_as_bytearray()

        self.logger.info("Editing photo with prompt: %s", prompt)

        # https://replicate.com/stability-ai/sdxl/versions/d830ba5dabf8090ec0db6c10fc862c6eb1c929e1a194a5411852d25fd954ac82
        inputs = {
            "image": io.BytesIO(image_bytes),
            "prompt": prompt,
            **options
        }
        inputs.update(self.config["replicate"]["models"]["pix2pix"])
        if "negative_prompt" in options:
            inputs["negative_prompt"] = options["negative_prompt"]
        if "nsfw_ok" in options:
            inputs["disable_safety_checker"] = True

        await typing_msg
        output = await self.replicate.async_run(model_name, input=inputs)
        self.logger.info("Done editing. Output: %s", json.dumps(output))
        if not isinstance(output, list):
            output = [output]
        if  len(output) == 1:
            await update.message.reply_photo(output[0], caption=f"Edited: {prompt}", write_timeout=None)
        elif len(output) > 1:
            await update.message.reply_media_group(
                [InputMediaPhoto(url) for url in output], caption=f"Edited: {prompt}"
            )
        else:
            await typing_msg
            raise Exception("Didn't get any output, that's weird")

    async def help(self, bot, update):
        bot_chat_id = update.message.chat.id
        if self.chat_ids != [] and bot_chat_id not in self.chat_ids:
            self.logger.warning("Unauthorized access from %d detected !", bot_chat_id)
            self.logger.warning(update)
        else:
            message = """
            ML Telegram bot Help.
            blah blah blah
            """
            await update.message.reply_markdown_v2(message)

    def parse_prompt(self, message: str):
        """Parses out the prompt and any additional properties (indicated with a |name=value suffix) from a message"""
        prompt = message
        properties = {}
        if "|" in prompt:
            prompt, properties = prompt.split("|", 1)
            properties = dict([prop.split("=", 1) for prop in properties.split("|")])
        return prompt, properties

    @contextlib.asynccontextmanager
    async def typing_notifications(self, message, action: ChatAction):
        async def typing_loop():
            while True:
                asyncio.create_task(message.reply_chat_action(action))
                await asyncio.sleep(3)

        task = asyncio.create_task(typing_loop())
        try:
            yield
        finally:
            task.cancel()

    async def error(self, update: Optional[object], context: CallbackContext):
        tb = "\n".join(traceback.format_exception(context.error))
        self.logger.warn('Update "%s" caused error "%s"', update, tb)
        if update.message.chat_id in self.chat_ids:
            preamble = escape_markdown("Whoops! Something went wrong...", version=2)
            max_len = 4096 - 16 - len(preamble)
            err = "```\n" + escape_markdown(tb, version=2)[-max_len:-1] + "\n```"
            msg = f"{preamble}\n\n{err}"
            self.logger.warn("Send err reply: %s", msg)
            await update.message.reply_text(msg, quote=True, parse_mode="MarkdownV2")

    async def download_image(self, url: str) -> bytes:
        async with httpx.AsyncClient() as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.content

    def load_config(self):
        self.logger.info("(Re)loading configuration")
        with open("config.yaml", "r", encoding="utf-8") as cfg:
            self.config = yaml.load(cfg, Loader=Loader)

            logging.getLogger().setLevel(int(self.config["log"]["level"]))

            # Warn if no chat_ids configured
            if "chat_ids" in self.config["bot"]:
                self.chat_ids = self.config["bot"]["chat_ids"]
                self.logger.info(
                    "chat_ids found. Only updates from your chat_ids will be taken care of."
                )
            else:
                self.logger.info(
                    "chat_ids not found. Anyone can interact with your chat. Proceed with caution."
                )

            self.replicate = replicate.Client(
                api_token=self.config["replicate"]["token"]
            )

    async def init(self):
        await self.storage.init()
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        await self.connections.run_server()

    async def shutdown(self):
        if self.shutdown_done:
            return
        self.shutdown_done = True
        self.logger.info("Shutting down")
        try:
            async with asyncio.timeout(10):
                await self.application.updater.stop()
                await self.application.stop()
                await self.application.shutdown()
        except:
            print("Exception when trying to stop application: " + repr(sys.exception()))
        loop = asyncio.get_event_loop()
        try:
            async with asyncio.timeout(5):
                self.logger.info("Shutting down asyncgens")
                await loop.create_task(loop.shutdown_asyncgens())
                self.logger.info("Cancelling all remaining tasks")
                tasks = [
                    t for t in asyncio.all_tasks() if t is not asyncio.current_task()
                ]
                [task.cancel() for task in tasks]
                self.logger.info("Waiting for tasks to complete")
                asyncio.gather(*tasks, return_exceptions=True)
        except:
            print(
                "Exception when trying to stop all remaining tasks: "
                + repr(sys.exception())
            )
        tasks_remaining = [task for task in tasks if not task.done()]
        self.logger.info("Tasks remaining after timeout: %s", tasks_remaining)
        loop.stop()
        sys.exit(1)

    def main(self):
        logging.basicConfig(
            format="%(asctime)s | %(levelname)s | %(module)s - %(message)s"
        )

        self.logger.warning("Starting ML Telegram Bot")

        self.load_config()
        bot_token = self.config["bot"]["token"]

        self.application = ApplicationBuilder().token(bot_token).build()
        self.storage = Storage(
            self.config.get("storage", {"url": "sqlite+aiosqlite:///:memory:"})
        )
        self.connections = Connections(self.config.get("connections", {}), self.storage)
        self.llmagent = LlmAgent(
            self.logger,
            self.config,
            self.storage,
            self.typing_notifications,
            self.chat_ids,
        )

        self.application.add_handler(
            CommandHandler("stablediffusion", self.stablediffusion)
        )
        self.application.add_handler(CommandHandler("dreamshaper", self.dreamshaper))
        self.application.add_handler(CommandHandler("help", self.help))
        self.application.add_handler(
            CommandHandler("connect", self.connections.handle_connect)
        )
        self.application.add_handler(
            MessageHandler(
                filters.PHOTO & filters.CaptionRegex(r"^[Ee]dit:\s*"), self.editphoto
            )
        )
        self.application.add_handler(
            MessageHandler(filters.TEXT, self.llmagent.chatbot)
        )

        self.application.add_error_handler(self.error)

        try:
            loop = asyncio.get_event_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(
                    signum, lambda: asyncio.create_task(self.shutdown())
                )
            loop.create_task(self.init())
            loop.run_forever()
        finally:
            if not self.shutdown_done:
                loop.create_task(self.shutdown())
