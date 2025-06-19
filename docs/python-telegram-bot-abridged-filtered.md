```text
---

# Hidden Headline

This is just here to get furo to display the right sidebar.

---

python-telegram-bot Logo PyPi Package Version Supported Python versions Supported Bot API version PyPi Package Monthly Download Documentation Status LGPLv3 License Github Actions workflow Code coverage Median time to resolve an issue Code quality: Codacy pre-commit.ci status Code Style: Black Telegram Channel Telegram Group We have made you a wrapper you can’t refuse

We have a vibrant community of developers helping each other in our Telegram group (https://telegram.me/pythontelegrambotgroup) . Join us!

*Stay tuned for library updates and new releases on our*Telegram Channel (https://t.me/pythontelegrambotchannel).

---

## Introduction

This library provides a pure Python, asynchronous interface for the Telegram Bot API (https://core.telegram.org/bots/api).
It’s compatible with Python versions **3.9+**.

In addition to the pure API implementation, this library features several convenience methods and shortcuts as well as a number of high-level classes to
make the development of bots easy and straightforward. These classes are contained in the `telegram.ext` submodule.

After installing the library, be sure to check out the section on working with PTB.

---

### Telegram API support

All types and methods of the Telegram Bot API **8.3**are natively supported by this library.
In addition, Bot API functionality not yet natively included can still be used as described in our wiki (https://github.com/python-telegram-bot/python-telegram-bot/wiki/Bot-API-Forward-Compatibility).

---

---

### Notable Features

- Fully asynchronous (https://github.com/python-telegram-bot/python-telegram-bot/wiki/Concurrency)
- Convenient shortcut methods, e.g. Message.reply_text (https://docs.python-telegram-bot.org/en/stable/telegram.message.html#telegram.Message.reply_text)
- Fully annotated with static type hints (https://github.com/python-telegram-bot/python-telegram-bot/wiki/Type-Checking)
- Customizable and extendable interface (https://github.com/python-telegram-bot/python-telegram-bot/wiki/Architecture)
- Seamless integration with webhooks (https://github.com/python-telegram-bot/python-telegram-bot/wiki/Webhooks) and polling (https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html#telegram.ext.Application.run_polling)
- Comprehensive documentation and examples (about:blank#working-with-ptb)

---

---

---

## Installing

You can install or upgrade `python-telegram-bot` via

```text
$ pip install python-telegram-bot --upgrade

```
To install a pre-release, use the `--pre` flag (https://pip.pypa.io/en/stable/cli/pip_install/#cmdoption-pre) in addition.

You can also install `python-telegram-bot` from source, though this is usually not necessary.

```text
$ git clone https://github.com/python-telegram-bot/python-telegram-bot

$ cd python-telegram-bot
$ pip install build
$ python -m build

```
---

### Verifying Releases

To enable you to verify that a release file that you downloaded was indeed provided by the `python-telegram-bot` team, we have taken the following measures.

Starting with v21.4, all releases are signed via sigstore (https://www.sigstore.dev/).
The corresponding signature files are uploaded to the GitHub releases page (https://github.com/python-telegram-bot/python-telegram-bot/releases).
To verify the signature, please install the sigstore Python client (https://pypi.org/project/sigstore/) and follow the instructions for verifying signatures from GitHub Actions (https://github.com/sigstore/sigstore-python?tab=readme-ov-file). As input for the `--repository` parameter, please use the value `python-telegram-bot/python-telegram-bot`.

Earlier releases are signed with a GPG key.
The signatures are uploaded to both the GitHub releases page (https://github.com/python-telegram-bot/python-telegram-bot/releases) and the PyPI project (https://pypi.org/project/python-telegram-bot/) and end with a suffix `.asc`.
Please find the public keys here (https://github.com/python-telegram-bot/python-telegram-bot/tree/master/public_keys).
The keys are named in the format `<first_version>-<last_version>.gpg`.

In addition, the GitHub release page also contains the sha1 hashes of the release files in the files with the suffix `.sha1`.

---

---

### Dependencies & Their Versions

`python-telegram-bot` tries to use as few 3rd party dependencies as possible.
However, for some features using a 3rd party library is more sane than implementing the functionality again.
As these features are *optional*, the corresponding 3rd party dependencies are not installed by default.
Instead, they are listed as optional dependencies.
This allows to avoid unnecessary dependency conflicts for users who don’t need the optional features.

The only required dependency is httpx ~= 0.27 (https://www.python-httpx.org/) for `telegram.request.HTTPXRequest` , the default networking backend.

`python-telegram-bot` is most useful when used along with additional libraries.
To minimize dependency conflicts, we try to be liberal in terms of version requirements on the (optional) dependencies.
On the other hand, we have to ensure stability of `python-telegram-bot` , which is why we do apply version bounds.
If you encounter dependency conflicts due to these bounds, feel free to reach out.

---

#### Optional Dependencies

PTB can be installed with optional dependencies:

- `pip install "python-telegram-bot[passport]"` installs the cryptography>=39.0.1 (https://cryptography.io/en/stable) library. Use this, if you want to use Telegram Passport related functionality.
- `pip install "python-telegram-bot[socks]"` installs httpx[socks] (https://www.python-httpx.org/#dependencies). Use this, if you want to work behind a Socks5 server.
- `pip install "python-telegram-bot[http2]"` installs httpx[http2] (https://www.python-httpx.org/#dependencies). Use this, if you want to use HTTP/2.
- `pip install "python-telegram-bot[rate-limiter]"` installs aiolimiter~=1.1,<1.3 (https://aiolimiter.readthedocs.io/en/stable/). Use this, if you want to use `telegram.ext.AIORateLimiter`.
- `pip install "python-telegram-bot[webhooks]"` installs the tornado~=6.4 (https://www.tornadoweb.org/en/stable/) library. Use this, if you want to use `telegram.ext.Updater.start_webhook` / `telegram.ext.Application.run_webhook`.
- `pip install "python-telegram-bot[callback-data]"` installs the cachetools>=5.3.3,<5.6.0 (https://cachetools.readthedocs.io/en/latest/) library. Use this, if you want to use arbitrary callback_data (https://github.com/python-telegram-bot/python-telegram-bot/wiki/Arbitrary-callback_data).
- `pip install "python-telegram-bot[job-queue]"` installs the APScheduler>=3.10.4,<3.12.0 (https://apscheduler.readthedocs.io/en/3.x/) library. Use this, if you want to use the `telegram.ext.JobQueue`.

To install multiple optional dependencies, separate them by commas, e.g. `pip install "python-telegram-bot[socks,webhooks]"`.

Additionally, two shortcuts are provided:

- `pip install "python-telegram-bot[all]"` installs all optional dependencies.
- `pip install "python-telegram-bot[ext]"` installs all optional dependencies that are related to `telegram.ext` , i.e. `[rate-limiter, webhooks, callback-data, job-queue]`.

---

---

---

---

## Working with PTB

Once you have installed the library, you can begin working with it - so let’s get started!

---

### Quick Start

Our Wiki contains an Introduction to the API (https://github.com/python-telegram-bot/python-telegram-bot/wiki/Introduction-to-the-API) explaining how the pure Bot API can be accessed via `python-telegram-bot`.
Moreover, the Tutorial: Your first Bot (https://github.com/python-telegram-bot/python-telegram-bot/wiki/Extensions---Your-first-Bot) gives an introduction on how chatbots can be easily programmed with the help of the `telegram.ext` module.

---

---

### Resources

- The package documentation (https://docs.python-telegram-bot.org/) is the technical reference for `python-telegram-bot`.
It contains descriptions of all available classes, modules, methods and arguments as well as the changelog (https://docs.python-telegram-bot.org/changelog.html).

- The wiki (https://github.com/python-telegram-bot/python-telegram-bot/wiki/) is home to number of more elaborate introductions of the different features of `python-telegram-bot` and other useful resources that go beyond the technical documentation.
- Our examples section (https://docs.python-telegram-bot.org/examples.html) contains several examples that showcase the different features of both the Bot API and `python-telegram-bot`.
Even if it is not your approach for learning, please take a look at `echobot.py`. It is the de facto base for most of the bots out there.
The code for these examples is released to the public domain, so you can start by grabbing the code and building on top of it.

- The official Telegram Bot API documentation (https://core.telegram.org/bots/api) is of course always worth a read.

---

---

### Getting help

If the resources mentioned above don’t answer your questions or simply overwhelm you, there are several ways of getting help.

1. We have a vibrant community of developers helping each other in our Telegram group (https://telegram.me/pythontelegrambotgroup). Join us! Asking a question here is often the quickest way to get a pointer in the right direction.
2. Ask questions by opening a discussion (https://github.com/python-telegram-bot/python-telegram-bot/discussions/new).
3. You can even ask for help on Stack Overflow (https://stackoverflow.com/questions/tagged/python-telegram-bot) using the python-telegram-bot tag (https://stackoverflow.com/questions/tagged/python-telegram-bot).

---

---

### Concurrency

Since v20.0, `python-telegram-bot` is built on top of Pythons `asyncio` module.
Because `asyncio` is in general single-threaded, `python-telegram-bot` does currently not aim to be thread-safe.
Noteworthy parts of `python-telegram-bots` API that are likely to cause issues (e.g. race conditions) when used in a multi-threaded setting include:

- `telegram.ext.Application/Updater.update_queue`
- `telegram.ext.ConversationHandler.check/handle_update`
- `telegram.ext.CallbackDataCache`
- `telegram.ext.BasePersistence`
- all classes in the `telegram.ext.filters` module that allow to add/remove allowed users/chats at runtime

---

---

---

---

## Contributing

Contributions of all sizes are welcome.
Please review our contribution guidelines (https://github.com/python-telegram-bot/python-telegram-bot/blob/master/.github/CONTRIBUTING.rst) to get started.
You can also help by reporting bugs or feature requests (https://github.com/python-telegram-bot/python-telegram-bot/issues/new/choose).

---

---

## Donating

Occasionally we are asked if we accept donations to support the development.
While we appreciate the thought, maintaining PTB is our hobby, and we have almost no running costs for it. We therefore have nothing set up to accept donations.
If you still want to donate, we kindly ask you to donate to another open source project/initiative of your choice instead.

---

---

## License

You may copy, distribute and modify the software provided that modifications are described and licensed for free under LGPL-3 (https://www.gnu.org/licenses/lgpl-3.0.html).
Derivative works (including modifications or anything statically linked to the library) can only be redistributed under LGPL-3, but applications that use the library don’t have to be.

---

---

# arbitrarycallbackdatabot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""This example showcases how PTBs "arbitrary callback data" feature can be used.

For detailed info on arbitrary callback data, see the wiki page at
https://github.com/python-telegram-bot/python-telegram-bot/wiki/Arbitrary-callback_data

Note:
To use arbitrary callback data, you must install PTB via
`pip install "python-telegram-bot[callback-data]"`
"""
import logging
from typing import cast

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InvalidCallbackData,
    PicklePersistence,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message with 5 inline buttons attached."""
    number_list: list[int] = []
    await update.message.reply_text("Please choose:", reply_markup=build_keyboard(number_list))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays info on how to use the bot."""
    await update.message.reply_text(
        "Use /start to test this bot. Use /clear to clear the stored data so that you can see "
        "what happens, if the button data is not available. "
    )

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clears the callback data cache"""
    context.bot.callback_data_cache.clear_callback_data()
    context.bot.callback_data_cache.clear_callback_queries()
    await update.effective_message.reply_text("All clear!")

def build_keyboard(current_list: list[int]) -> InlineKeyboardMarkup:
    """Helper function to build the next inline keyboard."""
    return InlineKeyboardMarkup.from_column(
        [InlineKeyboardButton(str(i), callback_data=(i, current_list)) for i in range(1, 6)]
    )

async def list_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query
    await query.answer()
    # Get the data from the callback_data.
    # If you're using a type checker like MyPy, you'll have to use typing.cast
    # to make the checker get the expected type of the callback_data
    number, number_list = cast(tuple[int, list[int]], query.data)
    # append the number to the list
    number_list.append(number)

    await query.edit_message_text(
        text=f"So far you've selected {number_list}. Choose the next item:",
        reply_markup=build_keyboard(number_list),
    )

    # we can delete the data stored for the query, because we've replaced the buttons
    context.drop_callback_data(query)

async def handle_invalid_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Informs the user that the button is no longer available."""
    await update.callback_query.answer()
    await update.effective_message.edit_text(
        "Sorry, I could not process this button click 😕 Please send /start to get a new keyboard."
    )

def main() -> None:
    """Run the bot."""
    # We use persistence to demonstrate how buttons can still work after the bot was restarted
    persistence = PicklePersistence(filepath="arbitrarycallbackdatabot")
    # Create the Application and pass it your bot's token.
    application = (
        Application.builder()
        .token("TOKEN")
        .persistence(persistence)
        .arbitrary_callback_data(True)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("clear", clear))
    application.add_handler(
        CallbackQueryHandler(handle_invalid_button, pattern=InvalidCallbackData)
    )
    application.add_handler(CallbackQueryHandler(list_button))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# chatmemberbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Simple Bot to handle '(my_)chat_member' updates.
Greets new users & keeps track of which chats the bot is in.

Usage:
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging
from typing import Optional

from telegram import Chat, ChatMember, ChatMemberUpdated, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Enable logging

logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

def extract_status_change(chat_member_update: ChatMemberUpdated) -> Optional[tuple[bool, bool]]:
    """Takes a ChatMemberUpdated instance and extracts whether the 'old_chat_member' was a member
    of the chat and whether the 'new_chat_member' is a member of the chat. Returns None, if
    the status didn't change.
    """
    status_change = chat_member_update.difference().get("status")
    old_is_member, new_is_member = chat_member_update.difference().get("is_member", (None, None))

    if status_change is None:
        return None

    old_status, new_status = status_change
    was_member = old_status in [
        ChatMember.MEMBER,
        ChatMember.OWNER,
        ChatMember.ADMINISTRATOR,
    ] or (old_status == ChatMember.RESTRICTED and old_is_member is True)
    is_member = new_status in [
        ChatMember.MEMBER,
        ChatMember.OWNER,
        ChatMember.ADMINISTRATOR,
    ] or (new_status == ChatMember.RESTRICTED and new_is_member is True)

    return was_member, is_member

async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tracks the chats the bot is in."""
    result = extract_status_change(update.my_chat_member)
    if result is None:
        return
    was_member, is_member = result

    # Let's check who is responsible for the change
    cause_name = update.effective_user.full_name

    # Handle chat types differently:
    chat = update.effective_chat
    if chat.type == Chat.PRIVATE:
        if not was_member and is_member:
            # This may not be really needed in practice because most clients will automatically
            # send a /start command after the user unblocks the bot, and start_private_chat()
            # will add the user to "user_ids".
            # We're including this here for the sake of the example.
            logger.info(" %s unblocked the bot", cause_name)
            context.bot_data.setdefault("user_ids", set()).add(chat.id)
        elif was_member and not is_member:
            logger.info(" %s blocked the bot", cause_name)
            context.bot_data.setdefault("user_ids", set()).discard(chat.id)
    elif chat.type in [Chat.GROUP, Chat.SUPERGROUP]:
        if not was_member and is_member:
            logger.info(" %s added the bot to the group %s ", cause_name, chat.title)
            context.bot_data.setdefault("group_ids", set()).add(chat.id)
        elif was_member and not is_member:
            logger.info(" %s removed the bot from the group %s ", cause_name, chat.title)
            context.bot_data.setdefault("group_ids", set()).discard(chat.id)
    elif not was_member and is_member:
        logger.info(" %s added the bot to the channel %s ", cause_name, chat.title)
        context.bot_data.setdefault("channel_ids", set()).add(chat.id)
    elif was_member and not is_member:
        logger.info(" %s removed the bot from the channel %s ", cause_name, chat.title)
        context.bot_data.setdefault("channel_ids", set()).discard(chat.id)

async def show_chats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows which chats the bot is in"""
    user_ids = ", ".join(str(uid) for uid in context.bot_data.setdefault("user_ids", set()))
    group_ids = ", ".join(str(gid) for gid in context.bot_data.setdefault("group_ids", set()))
    channel_ids = ", ".join(str(cid) for cid in context.bot_data.setdefault("channel_ids", set()))
    text = (
        f"@ {context.bot.username} is currently in a conversation with the user IDs {user_ids}."
        f" Moreover it is a member of the groups with IDs {group_ids} "
        f"and administrator in the channels with IDs {channel_ids}."
    )
    await update.effective_message.reply_text(text)

async def greet_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Greets new users in chats and announces when someone leaves"""
    result = extract_status_change(update.chat_member)
    if result is None:
        return

    was_member, is_member = result
    cause_name = update.chat_member.from_user.mention_html()
    member_name = update.chat_member.new_chat_member.user.mention_html()

    if not was_member and is_member:
        await update.effective_chat.send_message(
            f" {member_name} was added by {cause_name}. Welcome!",
            parse_mode=ParseMode.HTML,
        )
    elif was_member and not is_member:
        await update.effective_chat.send_message(
            f" {member_name} is no longer with us. Thanks a lot, {cause_name}...",
            parse_mode=ParseMode.HTML,
        )

async def start_private_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Greets the user and records that they started a chat with the bot if it's a private chat.
    Since no `my_chat_member` update is issued when a user starts a private chat with the bot
    for the first time, we have to track it explicitly here.
    """
    user_name = update.effective_user.full_name
    chat = update.effective_chat
    if chat.type != Chat.PRIVATE or chat.id in context.bot_data.get("user_ids", set()):
        return

    logger.info(" %s started a private chat with the bot", user_name)
    context.bot_data.setdefault("user_ids", set()).add(chat.id)

    await update.effective_message.reply_text(
        f"Welcome {user_name}. Use /show_chats to see what chats I'm in."
    )

def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # Keep track of which chats the bot is in
    application.add_handler(ChatMemberHandler(track_chats, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("show_chats", show_chats))

    # Handle members joining/leaving chats.
    application.add_handler(ChatMemberHandler(greet_chat_members, ChatMemberHandler.CHAT_MEMBER))

    # Interpret any other command or text message as a start of a private chat.
    # This will record the user as being in a private chat with bot.
    application.add_handler(MessageHandler(filters.ALL, start_private_chat))

    # Run the bot until the user presses Ctrl-C
    # We pass 'allowed_updates' handle *all*updates including `chat_member` updates
    # To reset this, simply pass `allowed_updates=[]`
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# contexttypesbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Simple Bot to showcase `telegram.ext.ContextTypes`.

Usage:
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging
from collections import defaultdict
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

class ChatData:
    """Custom class for chat_data. Here we store data per message."""

    def __init__(self) -> None:
        self.clicks_per_message: defaultdict[int, int] = defaultdict(int)

# The [ExtBot, dict, ChatData, dict] is for type checkers like mypy
class CustomContext(CallbackContext[ExtBot, dict, ChatData, dict]):
    """Custom class for context."""

    def __init__(
        self,
        application: Application,
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ):
        super().__init__(application=application, chat_id=chat_id, user_id=user_id)
        self._message_id: Optional[int] = None

    @property
    def bot_user_ids(self) -> set[int]:
        """Custom shortcut to access a value stored in the bot_data dict"""
        return self.bot_data.setdefault("user_ids", set())

    @property
    def message_clicks(self) -> Optional[int]:
        """Access the number of clicks for the message this context object was built for."""
        if self._message_id:
            return self.chat_data.clicks_per_message[self._message_id]
        return None

    @message_clicks.setter
    def message_clicks(self, value: int) -> None:
        """Allow to change the count"""
        if not self._message_id:
            raise RuntimeError("There is no message associated with this context object.")
        self.chat_data.clicks_per_message[self._message_id] = value

    @classmethod
    def from_update(cls, update: object, application: "Application") -> "CustomContext":
        """Override from_update to set _message_id."""
        # Make sure to call super()
        context = super().from_update(update, application)

        if context.chat_data and isinstance(update, Update) and update.effective_message:
            # pylint: disable=protected-access
            context._message_id = update.effective_message.message_id

        # Remember to return the object
        return context

async def start(update: Update, context: CustomContext) -> None:
    """Display a message with a button."""
    await update.message.reply_html(
        "This button was clicked <i>0</i> times.",
        reply_markup=InlineKeyboardMarkup.from_button(
            InlineKeyboardButton(text="Click me!", callback_data="button")
        ),
    )

async def count_click(update: Update, context: CustomContext) -> None:
    """Update the click count for the message."""
    context.message_clicks += 1
    await update.callback_query.answer()
    await update.effective_message.edit_text(
        text=f"This button was clicked <i> {context.message_clicks} </i> times.",
        reply_markup=InlineKeyboardMarkup.from_button(
            InlineKeyboardButton(text="Click me!", callback_data="button")
        ),
        parse_mode=ParseMode.HTML,
    )

async def print_users(update: Update, context: CustomContext) -> None:
    """Show which users have been using this bot."""
    await update.message.reply_text(
        f"The following user IDs have used this bot: {', '.join(map(str, context.bot_user_ids))} "
    )

async def track_users(update: Update, context: CustomContext) -> None:
    """Store the user id of the incoming update, if any."""
    if update.effective_user:
        context.bot_user_ids.add(update.effective_user.id)

def main() -> None:
    """Run the bot."""
    context_types = ContextTypes(context=CustomContext, chat_data=ChatData)
    application = Application.builder().token("TOKEN").context_types(context_types).build()

    # run track_users in its own group to not interfere with the user handlers
    application.add_handler(TypeHandler(Update, track_users), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(count_click))
    application.add_handler(CommandHandler("print_users", print_users))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# conversationbot2.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
First, a few callback functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Example of a bot-user conversation using ConversationHandler.
Send /start to initiate the conversation.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

CHOOSING, TYPING_REPLY, TYPING_CHOICE = range(3)

reply_keyboard = [
    ["Age", "Favourite colour"],
    ["Number of siblings", "Something else..."],
    ["Done"],
]
markup = ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)

def facts_to_str(user_data: dict[str, str]) -> str:
    """Helper function for formatting the gathered user info."""
    facts = [f" {key} - {value} " for key, value in user_data.items()]
    return " \n ".join(facts).join([" \n ", " \n "])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation and ask user for input."""
    await update.message.reply_text(
        "Hi! My name is Doctor Botter. I will hold a more complex conversation with you. "
        "Why don't you tell me something about yourself?",
        reply_markup=markup,
    )

    return CHOOSING

async def regular_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask the user for info about the selected predefined choice."""
    text = update.message.text
    context.user_data["choice"] = text
    await update.message.reply_text(f"Your {text.lower()}? Yes, I would love to hear about that!")

    return TYPING_REPLY

async def custom_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask the user for a description of a custom category."""
    await update.message.reply_text(
        'Alright, please send me the category first, for example "Most impressive skill"'
    )

    return TYPING_CHOICE

async def received_information(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store info provided by user and ask for the next category."""
    user_data = context.user_data
    text = update.message.text
    category = user_data["choice"]
    user_data[category] = text
    del user_data["choice"]

    await update.message.reply_text(
        "Neat! Just so you know, this is what you already told me:"
        f" {facts_to_str(user_data)} You can tell me more, or change your opinion"
        " on something.",
        reply_markup=markup,
    )

    return CHOOSING

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display the gathered info and end the conversation."""
    user_data = context.user_data
    if "choice" in user_data:
        del user_data["choice"]

    await update.message.reply_text(
        f"I learned these facts about you: {facts_to_str(user_data)} Until next time!",
        reply_markup=ReplyKeyboardRemove(),
    )

    user_data.clear()
    return ConversationHandler.END

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # Add conversation handler with the states CHOOSING, TYPING_CHOICE and TYPING_REPLY
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING: [
                MessageHandler(
                    filters.Regex("^(Age|Favourite colour|Number of siblings)$"), regular_choice
                ),
                MessageHandler(filters.Regex("^Something else...$"), custom_choice),
            ],
            TYPING_CHOICE: [
                MessageHandler(
                    filters.TEXT & ~(filters.COMMAND | filters.Regex("^Done$")), regular_choice
                )
            ],
            TYPING_REPLY: [
                MessageHandler(
                    filters.TEXT & ~(filters.COMMAND | filters.Regex("^Done$")),
                    received_information,
                )
            ],
        },
        fallbacks=[MessageHandler(filters.Regex("^Done$"), done)],
    )

    application.add_handler(conv_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

## State Diagram

(State Diagram describing the conversation flow follows)

---

---

# conversationbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
First, a few callback functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Example of a bot-user conversation using ConversationHandler.
Send /start to initiate the conversation.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

GENDER, PHOTO, LOCATION, BIO = range(4)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation and asks the user about their gender."""
    reply_keyboard = [["Boy", "Girl", "Other"]]

    await update.message.reply_text(
        "Hi! My name is Professor Bot. I will hold a conversation with you. "
        "Send /cancel to stop talking to me. \n\n "
        "Are you a boy or a girl?",
        reply_markup=ReplyKeyboardMarkup(
            reply_keyboard, one_time_keyboard=True, input_field_placeholder="Boy or Girl?"
        ),
    )

    return GENDER

async def gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the selected gender and asks for a photo."""
    user = update.message.from_user
    logger.info("Gender of %s: %s ", user.first_name, update.message.text)
    await update.message.reply_text(
        "I see! Please send me a photo of yourself, "
        "so I know what you look like, or send /skip if you don't want to.",
        reply_markup=ReplyKeyboardRemove(),
    )

    return PHOTO

async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the photo and asks for a location."""
    user = update.message.from_user
    photo_file = await update.message.photo[-1].get_file()
    await photo_file.download_to_drive("user_photo.jpg")
    logger.info("Photo of %s: %s ", user.first_name, "user_photo.jpg")
    await update.message.reply_text(
        "Gorgeous! Now, send me your location please, or send /skip if you don't want to."
    )

    return LOCATION

async def skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skips the photo and asks for a location."""
    user = update.message.from_user
    logger.info("User %s did not send a photo.", user.first_name)
    await update.message.reply_text(
        "I bet you look great! Now, send me your location please, or send /skip."
    )

    return LOCATION

async def location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the location and asks for some info about the user."""
    user = update.message.from_user
    user_location = update.message.location
    logger.info(
        "Location of %s: %f / %f ", user.first_name, user_location.latitude, user_location.longitude
    )
    await update.message.reply_text(
        "Maybe I can visit you sometime! At last, tell me something about yourself."
    )

    return BIO

async def skip_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skips the location and asks for info about the user."""
    user = update.message.from_user
    logger.info("User %s did not send a location.", user.first_name)
    await update.message.reply_text(
        "You seem a bit paranoid! At last, tell me something about yourself."
    )

    return BIO

async def bio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the info about the user and ends the conversation."""
    user = update.message.from_user
    logger.info("Bio of %s: %s ", user.first_name, update.message.text)
    await update.message.reply_text("Thank you! I hope we can talk again some day.")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name)
    await update.message.reply_text(
        "Bye! I hope we can talk again some day.", reply_markup=ReplyKeyboardRemove()
    )

    return ConversationHandler.END

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # Add conversation handler with the states GENDER, PHOTO, LOCATION and BIO
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            GENDER: [MessageHandler(filters.Regex("^(Boy|Girl|Other)$"), gender)],
            PHOTO: [MessageHandler(filters.PHOTO, photo), CommandHandler("skip", skip_photo)],
            LOCATION: [
                MessageHandler(filters.LOCATION, location),
                CommandHandler("skip", skip_location),
            ],
            BIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, bio)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(conv_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

## State Diagram

(State Diagram describing the conversation flow follows)

---

---

# customwebhookbot.py

This example is available for different web frameworks.
You can select your preferred framework by opening one of the tabs above the code example.

Hint

The following examples show how different Python web frameworks can be used alongside PTB.
This can be useful for two use cases:

1. For extending the functionality of your existing bot to handling updates of external services
2. For extending the functionality of your exisiting web application to also include chat bot functionality

How the PTB and web framework components of the examples below are viewed surely depends on which use case one has in mind.
We are fully aware that a combination of PTB with web frameworks will always mean finding a tradeoff between usability and best practices for both PTB and the web framework and these examples are certainly far from optimal solutions.
Please understand them as starting points and use your expertise of the web framework of your choosing to build up on them.
You are of course also very welcome to help improve these examples!

starlette

```python
#!/usr/bin/env python

# This program is dedicated to the public domain under the CC0 license.
# pylint: disable=import-error,unused-argument
"""
Simple example of a bot that uses a custom webhook setup and handles custom updates.
For the custom webhook setup, the libraries `starlette` and `uvicorn` are used. Please install
them as `pip install starlette~=0.20.0 uvicorn~=0.23.2`.
Note that any other `asyncio` based web server framework can be used for a custom webhook setup
just as well.

Usage:
Set bot Token, URL, admin CHAT_ID and PORT after the imports.
You may also need to change the `listen` value in the uvicorn configuration to match your setup.
Press Ctrl-C on the command line or send a signal to the process to stop the bot.
"""
import asyncio
import html
import logging
from dataclasses import dataclass
from http import HTTPStatus

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define configuration constants
URL = "https://domain.tld"
ADMIN_CHAT_ID = 123456
PORT = 8000
TOKEN = "123:ABC" # nosec B105

@dataclass
class WebhookUpdate:
    """Simple dataclass to wrap a custom update type"""

    user_id: int
    payload: str

class CustomContext(CallbackContext[ExtBot, dict, dict, dict]):
    """
    Custom CallbackContext class that makes `user_data` available for updates of type
    `WebhookUpdate`.
    """

    @classmethod
    def from_update(
        cls,
        update: object,
        application: "Application",
    ) -> "CustomContext":
        if isinstance(update, WebhookUpdate):
            return cls(application=application, user_id=update.user_id)
        return super().from_update(update, application)

async def start(update: Update, context: CustomContext) -> None:
    """Display a message with instructions on how to use this bot."""
    payload_url = html.escape(f" {URL} /submitpayload?user_id=<your user id>&payload=<payload>")
    text = (
        f"To check if the bot is still running, call <code> {URL} /healthcheck</code>. \n\n "
        f"To post a custom update, call <code> {payload_url} </code>."
    )
    await update.message.reply_html(text=text)

async def webhook_update(update: WebhookUpdate, context: CustomContext) -> None:
    """Handle custom updates."""
    chat_member = await context.bot.get_chat_member(chat_id=update.user_id, user_id=update.user_id)
    payloads = context.user_data.setdefault("payloads", [])
    payloads.append(update.payload)
    combined_payloads = "</code> \n • <code>".join(payloads)
    text = (
        f"The user {chat_member.user.mention_html()} has sent a new payload. "
        f"So far they have sent the following payloads: \n\n • <code> {combined_payloads} </code>"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)

async def main() -> None:
    """Set up PTB application and a web application for handling the incoming requests."""
    context_types = ContextTypes(context=CustomContext)
    # Here we set updater to None because we want our custom webhook server to handle the updates
    # and hence we don't need an Updater instance
    application = (
        Application.builder().token(TOKEN).updater(None).context_types(context_types).build()
    )

    # register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(TypeHandler(type=WebhookUpdate, callback=webhook_update))

    # Pass webhook settings to telegram
    await application.bot.set_webhook(url=f" {URL} /telegram", allowed_updates=Update.ALL_TYPES)

    # Set up webserver
    async def telegram(request: Request) -> Response:
        """Handle incoming Telegram updates by putting them into the `update_queue`"""
        await application.update_queue.put(
            Update.de_json(data=await request.json(), bot=application.bot)
        )
        return Response()

    async def custom_updates(request: Request) -> PlainTextResponse:
        """
        Handle incoming webhook updates by also putting them into the `update_queue` if
        the required parameters were passed correctly.
        """
        try:
            user_id = int(request.query_params["user_id"])
            payload = request.query_params["payload"]
        except KeyError:
            return PlainTextResponse(
                status_code=HTTPStatus.BAD_REQUEST,
                content="Please pass both `user_id` and `payload` as query parameters.",
            )
        except ValueError:
            return PlainTextResponse(
                status_code=HTTPStatus.BAD_REQUEST,
                content="The `user_id` must be a string!",
            )

        await application.update_queue.put(WebhookUpdate(user_id=user_id, payload=payload))
        return PlainTextResponse("Thank you for the submission! It's being forwarded.")

    async def health(_: Request) -> PlainTextResponse:
        """For the health endpoint, reply with a simple plain text message."""
        return PlainTextResponse(content="The bot is still running fine :)")

    starlette_app = Starlette(
        routes=[
            Route("/telegram", telegram, methods=["POST"]),
            Route("/healthcheck", health, methods=["GET"]),
            Route("/submitpayload", custom_updates, methods=["POST", "GET"]),
        ]
    )
    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=starlette_app,
            port=PORT,
            use_colors=False,
            host="127.0.0.1",
        )
    )

    # Run application and webserver together
    async with application:
        await application.start()
        await webserver.serve()
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())

```
flask

```python
#!/usr/bin/env python

# This program is dedicated to the public domain under the CC0 license.
# pylint: disable=import-error,unused-argument
"""
Simple example of a bot that uses a custom webhook setup and handles custom updates.
For the custom webhook setup, the libraries `flask`, `asgiref` and `uvicorn` are used. Please
install them as `pip install flask[async]~=2.3.2 uvicorn~=0.23.2 asgiref~=3.7.2`.
Note that any other `asyncio` based web server framework can be used for a custom webhook setup
just as well.

Usage:
Set bot Token, URL, admin CHAT_ID and PORT after the imports.
You may also need to change the `listen` value in the uvicorn configuration to match your setup.
Press Ctrl-C on the command line or send a signal to the process to stop the bot.
"""
import asyncio
import html
import logging
from dataclasses import dataclass
from http import HTTPStatus

import uvicorn
from asgiref.wsgi import WsgiToAsgi
from flask import Flask, Response, abort, make_response, request

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define configuration constants
URL = "https://domain.tld"
ADMIN_CHAT_ID = 123456
PORT = 8000
TOKEN = "123:ABC" # nosec B105

@dataclass
class WebhookUpdate:
    """Simple dataclass to wrap a custom update type"""

    user_id: int
    payload: str

class CustomContext(CallbackContext[ExtBot, dict, dict, dict]):
    """
    Custom CallbackContext class that makes `user_data` available for updates of type
    `WebhookUpdate`.
    """

    @classmethod
    def from_update(
        cls,
        update: object,
        application: "Application",
    ) -> "CustomContext":
        if isinstance(update, WebhookUpdate):
            return cls(application=application, user_id=update.user_id)
        return super().from_update(update, application)

async def start(update: Update, context: CustomContext) -> None:
    """Display a message with instructions on how to use this bot."""
    payload_url = html.escape(f" {URL} /submitpayload?user_id=<your user id>&payload=<payload>")
    text = (
        f"To check if the bot is still running, call <code> {URL} /healthcheck</code>. \n\n "
        f"To post a custom update, call <code> {payload_url} </code>."
    )
    await update.message.reply_html(text=text)

async def webhook_update(update: WebhookUpdate, context: CustomContext) -> None:
    """Handle custom updates."""
    chat_member = await context.bot.get_chat_member(chat_id=update.user_id, user_id=update.user_id)
    payloads = context.user_data.setdefault("payloads", [])
    payloads.append(update.payload)
    combined_payloads = "</code> \n • <code>".join(payloads)
    text = (
        f"The user {chat_member.user.mention_html()} has sent a new payload. "
        f"So far they have sent the following payloads: \n\n • <code> {combined_payloads} </code>"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)

async def main() -> None:
    """Set up PTB application and a web application for handling the incoming requests."""
    context_types = ContextTypes(context=CustomContext)
    # Here we set updater to None because we want our custom webhook server to handle the updates
    # and hence we don't need an Updater instance
    application = (
        Application.builder().token(TOKEN).updater(None).context_types(context_types).build()
    )

    # register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(TypeHandler(type=WebhookUpdate, callback=webhook_update))

    # Pass webhook settings to telegram
    await application.bot.set_webhook(url=f" {URL} /telegram", allowed_updates=Update.ALL_TYPES)

    # Set up webserver
    flask_app = Flask(__name__)

    @flask_app.post("/telegram") # type: ignore[misc]
    async def telegram() -> Response:
        """Handle incoming Telegram updates by putting them into the `update_queue`"""
        await application.update_queue.put(Update.de_json(data=request.json, bot=application.bot))
        return Response(status=HTTPStatus.OK)

    @flask_app.route("/submitpayload", methods=["GET", "POST"]) # type: ignore[misc]
    async def custom_updates() -> Response:
        """
        Handle incoming webhook updates by also putting them into the `update_queue` if
        the required parameters were passed correctly.
        """
        try:
            user_id = int(request.args["user_id"])
            payload = request.args["payload"]
        except KeyError:
            abort(
                HTTPStatus.BAD_REQUEST,
                "Please pass both `user_id` and `payload` as query parameters.",
            )
        except ValueError:
            abort(HTTPStatus.BAD_REQUEST, "The `user_id` must be a string!")

        await application.update_queue.put(WebhookUpdate(user_id=user_id, payload=payload))
        return Response(status=HTTPStatus.OK)

    @flask_app.get("/healthcheck") # type: ignore[misc]
    async def health() -> Response:
        """For the health endpoint, reply with a simple plain text message."""
        response = make_response("The bot is still running fine :)", HTTPStatus.OK)
        response.mimetype = "text/plain"
        return response

    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=WsgiToAsgi(flask_app),
            port=PORT,
            use_colors=False,
            host="127.0.0.1",
        )
    )

    # Run application and webserver together
    async with application:
        await application.start()
        await webserver.serve()
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())

```
quart

```python
#!/usr/bin/env python

# This program is dedicated to the public domain under the CC0 license.
# pylint: disable=import-error,unused-argument
"""
Simple example of a bot that uses a custom webhook setup and handles custom updates.
For the custom webhook setup, the libraries `quart` and `uvicorn` are used. Please
install them as `pip install quart~=0.18.4 uvicorn~=0.23.2`.
Note that any other `asyncio` based web server framework can be used for a custom webhook setup
just as well.

Usage:
Set bot Token, URL, admin CHAT_ID and PORT after the imports.
You may also need to change the `listen` value in the uvicorn configuration to match your setup.
Press Ctrl-C on the command line or send a signal to the process to stop the bot.
"""
import asyncio
import html
import logging
from dataclasses import dataclass
from http import HTTPStatus

import uvicorn
from quart import Quart, Response, abort, make_response, request

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define configuration constants
URL = "https://domain.tld"
ADMIN_CHAT_ID = 123456
PORT = 8000
TOKEN = "123:ABC" # nosec B105

@dataclass
class WebhookUpdate:
    """Simple dataclass to wrap a custom update type"""

    user_id: int
    payload: str

class CustomContext(CallbackContext[ExtBot, dict, dict, dict]):
    """
    Custom CallbackContext class that makes `user_data` available for updates of type
    `WebhookUpdate`.
    """

    @classmethod
    def from_update(
        cls,
        update: object,
        application: "Application",
    ) -> "CustomContext":
        if isinstance(update, WebhookUpdate):
            return cls(application=application, user_id=update.user_id)
        return super().from_update(update, application)

async def start(update: Update, context: CustomContext) -> None:
    """Display a message with instructions on how to use this bot."""
    payload_url = html.escape(f" {URL} /submitpayload?user_id=<your user id>&payload=<payload>")
    text = (
        f"To check if the bot is still running, call <code> {URL} /healthcheck</code>. \n\n "
        f"To post a custom update, call <code> {payload_url} </code>."
    )
    await update.message.reply_html(text=text)

async def webhook_update(update: WebhookUpdate, context: CustomContext) -> None:
    """Handle custom updates."""
    chat_member = await context.bot.get_chat_member(chat_id=update.user_id, user_id=update.user_id)
    payloads = context.user_data.setdefault("payloads", [])
    payloads.append(update.payload)
    combined_payloads = "</code> \n • <code>".join(payloads)
    text = (
        f"The user {chat_member.user.mention_html()} has sent a new payload. "
        f"So far they have sent the following payloads: \n\n • <code> {combined_payloads} </code>"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)

async def main() -> None:
    """Set up PTB application and a web application for handling the incoming requests."""
    context_types = ContextTypes(context=CustomContext)
    # Here we set updater to None because we want our custom webhook server to handle the updates
    # and hence we don't need an Updater instance
    application = (
        Application.builder().token(TOKEN).updater(None).context_types(context_types).build()
    )

    # register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(TypeHandler(type=WebhookUpdate, callback=webhook_update))

    # Pass webhook settings to telegram
    await application.bot.set_webhook(url=f" {URL} /telegram", allowed_updates=Update.ALL_TYPES)

    # Set up webserver
    quart_app = Quart(__name__)

    @quart_app.post("/telegram") # type: ignore[misc]
    async def telegram() -> Response:
        """Handle incoming Telegram updates by putting them into the `update_queue`"""
        await application.update_queue.put(
            Update.de_json(data=await request.get_json(), bot=application.bot)
        )
        return Response(status=HTTPStatus.OK)

    @quart_app.route("/submitpayload", methods=["GET", "POST"]) # type: ignore[misc]
    async def custom_updates() -> Response:
        """
        Handle incoming webhook updates by also putting them into the `update_queue` if
        the required parameters were passed correctly.
        """
        try:
            user_id = int(request.args["user_id"])
            payload = request.args["payload"]
        except KeyError:
            abort(
                HTTPStatus.BAD_REQUEST,
                "Please pass both `user_id` and `payload` as query parameters.",
            )
        except ValueError:
            abort(HTTPStatus.BAD_REQUEST, "The `user_id` must be a string!")

        await application.update_queue.put(WebhookUpdate(user_id=user_id, payload=payload))
        return Response(status=HTTPStatus.OK)

    @quart_app.get("/healthcheck") # type: ignore[misc]
    async def health() -> Response:
        """For the health endpoint, reply with a simple plain text message."""
        response = await make_response("The bot is still running fine :)", HTTPStatus.OK)
        response.mimetype = "text/plain"
        return response

    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=quart_app,
            port=PORT,
            use_colors=False,
            host="127.0.0.1",
        )
    )

    # Run application and webserver together
    async with application:
        await application.start()
        await webserver.serve()
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())

```
Django

```python
#!/usr/bin/env python

# This program is dedicated to the public domain under the CC0 license.
# pylint: disable=import-error,unused-argument
"""
Simple example of a bot that uses a custom webhook setup and handles custom updates.
For the custom webhook setup, the libraries `Django` and `uvicorn` are used. Please
install them as `pip install Django~=4.2.4 uvicorn~=0.23.2`.
Note that any other `asyncio` based web server framework can be used for a custom webhook setup
just as well.

Usage:
Set bot Token, URL, admin CHAT_ID and PORT after the imports.
You may also need to change the `listen` value in the uvicorn configuration to match your setup.
Press Ctrl-C on the command line or send a signal to the process to stop the bot.
"""
import asyncio
import html
import json
import logging
from dataclasses import dataclass
from uuid import uuid4

import uvicorn
from django.conf import settings
from django.core.asgi import get_asgi_application
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.urls import path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define configuration constants
URL = "https://domain.tld"
ADMIN_CHAT_ID = 123456
PORT = 8000
TOKEN = "123:ABC" # nosec B105

@dataclass
class WebhookUpdate:
    """Simple dataclass to wrap a custom update type"""

    user_id: int
    payload: str

class CustomContext(CallbackContext[ExtBot, dict, dict, dict]):
    """
    Custom CallbackContext class that makes `user_data` available for updates of type
    `WebhookUpdate`.
    """

    @classmethod
    def from_update(
        cls,
        update: object,
        application: "Application",
    ) -> "CustomContext":
        if isinstance(update, WebhookUpdate):
            return cls(application=application, user_id=update.user_id)
        return super().from_update(update, application)

async def start(update: Update, context: CustomContext) -> None:
    """Display a message with instructions on how to use this bot."""
    payload_url = html.escape(f" {URL} /submitpayload?user_id=<your user id>&payload=<payload>")
    text = (
        f"To check if the bot is still running, call <code> {URL} /healthcheck</code>. \n\n "
        f"To post a custom update, call <code> {payload_url} </code>."
    )
    await update.message.reply_html(text=text)

async def webhook_update(update: WebhookUpdate, context: CustomContext) -> None:
    """Handle custom updates."""
    chat_member = await context.bot.get_chat_member(chat_id=update.user_id, user_id=update.user_id)
    payloads = context.user_data.setdefault("payloads", [])
    payloads.append(update.payload)
    combined_payloads = "</code> \n • <code>".join(payloads)
    text = (
        f"The user {chat_member.user.mention_html()} has sent a new payload. "
        f"So far they have sent the following payloads: \n\n • <code> {combined_payloads} </code>"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)

async def telegram(request: HttpRequest) -> HttpResponse:
    """Handle incoming Telegram updates by putting them into the `update_queue`"""
    await ptb_application.update_queue.put(
        Update.de_json(data=json.loads(request.body), bot=ptb_application.bot)
    )
    return HttpResponse()

async def custom_updates(request: HttpRequest) -> HttpResponse:
    """
    Handle incoming webhook updates by also putting them into the `update_queue` if
    the required parameters were passed correctly.
    """
    try:
        user_id = int(request.GET["user_id"])
        payload = request.GET["payload"]
    except KeyError:
        return HttpResponseBadRequest(
            "Please pass both `user_id` and `payload` as query parameters.",
        )
    except ValueError:
        return HttpResponseBadRequest("The `user_id` must be a string!")

    await ptb_application.update_queue.put(WebhookUpdate(user_id=user_id, payload=payload))
    return HttpResponse()

async def health(_: HttpRequest) -> HttpResponse:
    """For the health endpoint, reply with a simple plain text message."""
    return HttpResponse("The bot is still running fine :)")

# Set up PTB application and a web application for handling the incoming requests.

context_types = ContextTypes(context=CustomContext)

# Here we set updater to None because we want our custom webhook server to handle the updates
# and hence we don't need an Updater instance
ptb_application = (
    Application.builder().token(TOKEN).updater(None).context_types(context_types).build()
)

# register handlers
ptb_application.add_handler(CommandHandler("start", start))
ptb_application.add_handler(TypeHandler(type=WebhookUpdate, callback=webhook_update))

urlpatterns = [
    path("telegram", telegram, name="Telegram updates"),
    path("submitpayload", custom_updates, name="custom updates"),
    path("healthcheck", health, name="health check"),
]
settings.configure(ROOT_URLCONF=__name__, SECRET_KEY=uuid4().hex)

async def main() -> None:
    """Finalize configuration and run the applications."""
    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=get_asgi_application(),
            port=PORT,
            use_colors=False,
            host="127.0.0.1",
        )
    )

    # Pass webhook settings to telegram
    await ptb_application.bot.set_webhook(url=f" {URL} /telegram", allowed_updates=Update.ALL_TYPES)

    # Run application and webserver together
    async with ptb_application:
        await ptb_application.start()
        await webserver.serve()
        await ptb_application.stop()

if __name__ == "__main__":
    asyncio.run(main())

```

---

---

# deeplinking.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""Bot that explains Telegram's "Deep Linking Parameters" functionality.

This program is dedicated to the public domain under the CC0 license.

This Bot uses the Application class to handle the bot.

First, a few handler functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Deep Linking example. Send /start to get the link.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
    helpers,
)
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define constants that will allow us to reuse the deep-linking parameters.
CHECK_THIS_OUT = "check-this-out"
USING_ENTITIES = "using-entities-here"
USING_KEYBOARD = "using-keyboard-here"
SO_COOL = "so-cool"

# Callback data to pass in 3rd level deep-linking
KEYBOARD_CALLBACKDATA = "keyboard-callback-data"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a deep-linked URL when the command /start is issued."""
    bot = context.bot
    url = helpers.create_deep_linked_url(bot.username, CHECK_THIS_OUT, group=True)
    text = "Feel free to tell your friends about it: \n\n " + url
    await update.message.reply_text(text)

async def deep_linked_level_1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reached through the CHECK_THIS_OUT payload"""
    bot = context.bot
    url = helpers.create_deep_linked_url(bot.username, SO_COOL)
    text = (
        "Awesome, you just accessed hidden functionality! Now let's get back to the private chat."
    )
    keyboard = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton(text="Continue here!", url=url)
    )
    await update.message.reply_text(text, reply_markup=keyboard)

async def deep_linked_level_2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reached through the SO_COOL payload"""
    bot = context.bot
    url = helpers.create_deep_linked_url(bot.username, USING_ENTITIES)
    text = f'You can also mask the deep-linked URLs as links: <a href=" {url} ">▶️ CLICK HERE</a>.'
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

async def deep_linked_level_3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reached through the USING_ENTITIES payload"""
    await update.message.reply_text(
        "It is also possible to make deep-linking using InlineKeyboardButtons.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="Like this!", callback_data=KEYBOARD_CALLBACKDATA)]]
        ),
    )

async def deep_link_level_3_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answers CallbackQuery with deeplinking url."""
    bot = context.bot
    url = helpers.create_deep_linked_url(bot.username, USING_KEYBOARD)
    await update.callback_query.answer(url=url)

async def deep_linked_level_4(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reached through the USING_KEYBOARD payload"""
    payload = context.args
    await update.message.reply_text(
        f"Congratulations! This is as deep as it gets 👏🏻 \n\n The payload was: {payload} "
    )

def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # More info on what deep linking actually is (read this first if it's unclear to you):
    # https://core.telegram.org/bots/features#deep-linking

    # Register a deep-linking handler
    application.add_handler(
        CommandHandler("start", deep_linked_level_1, filters.Regex(CHECK_THIS_OUT))
    )

    # This one works with a textual link instead of an URL
    application.add_handler(CommandHandler("start", deep_linked_level_2, filters.Regex(SO_COOL)))

    # We can also pass on the deep-linking payload
    application.add_handler(
        CommandHandler("start", deep_linked_level_3, filters.Regex(USING_ENTITIES))
    )

    # Possible with inline keyboard buttons as well
    application.add_handler(
        CommandHandler("start", deep_linked_level_4, filters.Regex(USING_KEYBOARD))
    )

    # register callback handler for inline keyboard button
    application.add_handler(
        CallbackQueryHandler(deep_link_level_3_callback, pattern=KEYBOARD_CALLBACKDATA)
    )

    # Make sure the deep-linking handlers occur *before*the normal /start handler.
    application.add_handler(CommandHandler("start", start))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# echobot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Simple Bot to reply to Telegram messages.

First, a few handler functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Basic Echobot example, repeats messages.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging

from telegram import ForceReply, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define a few command handlers. These usually take the two arguments update and
# context.
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}!",
        reply_markup=ForceReply(selective=True),
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text("Help!")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Echo the user message."""
    await update.message.reply_text(update.message.text)

def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # on non command i.e message - echo the message on Telegram
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# errorhandlerbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""This is a very simple example on how one could implement a custom error handler."""
import html
import json
import logging
import traceback

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# This can be your own ID, or one for a developer group/channel.
# You can use the /start command of this bot to see your chat id.
DEVELOPER_CHAT_ID = 123456789

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error("Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    # Build the message with some markup and additional information about what happened.
    # You might need to add some logic to deal with messages longer than the 4096 character limit.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        "An exception was raised while handling an update \n "
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
        "</pre> \n\n "
        f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre> \n\n "
        f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre> \n\n "
        f"<pre> {html.escape(tb_string)}</pre>"
    )

    # Finally, send the message
    await context.bot.send_message(
        chat_id=DEVELOPER_CHAT_ID, text=message, parse_mode=ParseMode.HTML
    )

async def bad_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Raise an error to trigger the error handler."""
    await context.bot.wrong_method_name() # type: ignore[attr-defined]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays info on how to trigger an error."""
    await update.effective_message.reply_html(
        "Use /bad_command to cause an error. \n "
        f"Your chat id is <code> {update.effective_chat.id} </code>."
    )

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # Register the commands...
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("bad_command", bad_command))

    # ...and the error handler
    application.add_error_handler(error_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# inlinebot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Don't forget to enable inline mode with @BotFather

First, a few handler functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Basic inline bot example. Applies different text transformations.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""
import logging
from html import escape
from uuid import uuid4

from telegram import InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, InlineQueryHandler

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define a few command handlers. These usually take the two arguments update and
# context.
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    await update.message.reply_text("Hi!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text("Help!")

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the inline query. This is run when you type: @botusername <query>"""
    query = update.inline_query.query

    if not query: # empty query should not be handled
        return

    results = [
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="Caps",
            input_message_content=InputTextMessageContent(query.upper()),
        ),
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="Bold",
            input_message_content=InputTextMessageContent(
                f"<b> {escape(query)} </b>", parse_mode=ParseMode.HTML
            ),
        ),
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="Italic",
            input_message_content=InputTextMessageContent(
                f"<i> {escape(query)} </i>", parse_mode=ParseMode.HTML
            ),
        ),
    ]

    await update.inline_query.answer(results)

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # on inline queries - show corresponding inline results
    application.add_handler(InlineQueryHandler(inline_query))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# inlinekeyboard2.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""Simple inline keyboard bot with multiple CallbackQueryHandlers.

This Bot uses the Application class to handle the bot.
First, a few callback functions are defined as callback query handler. Then, those functions are
passed to the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.
Usage:
Example of a bot that uses inline keyboard that has multiple CallbackQueryHandlers arranged in a
ConversationHandler.
Send /start to initiate the conversation.
Press Ctrl-C on the command line to stop the bot.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Stages
START_ROUTES, END_ROUTES = range(2)

# Callback data
ONE, TWO, THREE, FOUR = range(4)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send message on `/start`."""
    # Get user that sent /start and log his name
    user = update.message.from_user
    logger.info("User %s started the conversation.", user.first_name)
    # Build InlineKeyboard where each button has a displayed text
    # and a string as callback_data
    # The keyboard is a list of button rows, where each row is in turn
    # a list (hence `[[...]]`).
    keyboard = [
        [
            InlineKeyboardButton("1", callback_data=str(ONE)),
            InlineKeyboardButton("2", callback_data=str(TWO)),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # Send message with text and appended InlineKeyboard
    await update.message.reply_text("Start handler, Choose a route", reply_markup=reply_markup)
    # Tell ConversationHandler that we're in state `FIRST` now
    return START_ROUTES

async def start_over(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt same text & keyboard as `start` does but not as new message"""
    # Get CallbackQuery from Update
    query = update.callback_query
    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("1", callback_data=str(ONE)),
            InlineKeyboardButton("2", callback_data=str(TWO)),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # Instead of sending a new message, edit the message that
    # originated the CallbackQuery. This gives the feeling of an
    # interactive menu.
    await query.edit_message_text(text="Start handler, Choose a route", reply_markup=reply_markup)
    return START_ROUTES

async def one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show new choice of buttons"""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("3", callback_data=str(THREE)),
            InlineKeyboardButton("4", callback_data=str(FOUR)),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text="First CallbackQueryHandler, Choose a route", reply_markup=reply_markup
    )
    return START_ROUTES

async def two(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show new choice of buttons"""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("1", callback_data=str(ONE)),
            InlineKeyboardButton("3", callback_data=str(THREE)),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text="Second CallbackQueryHandler, Choose a route", reply_markup=reply_markup
    )
    return START_ROUTES

async def three(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show new choice of buttons. This is the end point of the conversation."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("Yes, let's do it again!", callback_data=str(ONE)),
            InlineKeyboardButton("Nah, I've had enough ...", callback_data=str(TWO)),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text="Third CallbackQueryHandler. Do want to start over?", reply_markup=reply_markup
    )
    # Transfer to conversation state `SECOND`
    return END_ROUTES

async def four(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show new choice of buttons"""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("2", callback_data=str(TWO)),
            InlineKeyboardButton("3", callback_data=str(THREE)),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text="Fourth CallbackQueryHandler, Choose a route", reply_markup=reply_markup
    )
    return START_ROUTES

async def end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Returns `ConversationHandler.END`, which tells the
    ConversationHandler that the conversation is over.
    """
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="See you next time!")
    return ConversationHandler.END

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # Setup conversation handler with the states FIRST and SECOND
    # Use the pattern parameter to pass CallbackQueries with specific
    # data pattern to the corresponding handlers.
    # ^ means "start of line/string"
    # $ means "end of line/string"
    # So ^ABC$ will only allow 'ABC'
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            START_ROUTES: [
                CallbackQueryHandler(one, pattern="^" + str(ONE) + "$"),
                CallbackQueryHandler(two, pattern="^" + str(TWO) + "$"),
                CallbackQueryHandler(three, pattern="^" + str(THREE) + "$"),
                CallbackQueryHandler(four, pattern="^" + str(FOUR) + "$"),
            ],
            END_ROUTES: [
                CallbackQueryHandler(start_over, pattern="^" + str(ONE) + "$"),
                CallbackQueryHandler(end, pattern="^" + str(TWO) + "$"),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    # Add ConversationHandler to application that will be used for handling updates
    application.add_handler(conv_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# inlinekeyboard.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Basic example for a bot that uses inline keyboards. For an in-depth explanation, check out
https://github.com/python-telegram-bot/python-telegram-bot/wiki/InlineKeyboard-Example.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message with three inline buttons attached."""
    keyboard = [
        [
            InlineKeyboardButton("Option 1", callback_data="1"),
            InlineKeyboardButton("Option 2", callback_data="2"),
        ],
        [InlineKeyboardButton("Option 3", callback_data="3")],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("Please choose:", reply_markup=reply_markup)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query

    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()

    await query.edit_message_text(text=f"Selected option: {query.data}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays info on how to use the bot."""
    await update.message.reply_text("Use /start to test this bot.")

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(CommandHandler("help", help_command))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# nestedconversationbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
First, a few callback functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Example of a bot-user conversation using nested ConversationHandlers.
Send /start to initiate the conversation.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# State definitions for top level conversation
SELECTING_ACTION, ADDING_MEMBER, ADDING_SELF, DESCRIBING_SELF = map(chr, range(4))

# State definitions for second level conversation
SELECTING_LEVEL, SELECTING_GENDER = map(chr, range(4, 6))

# State definitions for descriptions conversation
SELECTING_FEATURE, TYPING = map(chr, range(6, 8))

# Meta states
STOPPING, SHOWING = map(chr, range(8, 10))

# Shortcut for ConversationHandler.END
END = ConversationHandler.END

# Different constants for this example
(
    PARENTS,
    CHILDREN,
    SELF,
    GENDER,
    MALE,
    FEMALE,
    AGE,
    NAME,
    START_OVER,
    FEATURES,
    CURRENT_FEATURE,
    CURRENT_LEVEL,
) = map(chr, range(10, 22))

# Helper
def _name_switcher(level: str) -> tuple[str, str]:
    if level == PARENTS:
        return "Father", "Mother"
    return "Brother", "Sister"

# Top level conversation callbacks
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Select an action: Adding parent/child or show data."""
    text = (
        "You may choose to add a family member, yourself, show the gathered data, or end the "
        "conversation. To abort, simply type /stop."
    )

    buttons = [
        [
            InlineKeyboardButton(text="Add family member", callback_data=str(ADDING_MEMBER)),
            InlineKeyboardButton(text="Add yourself", callback_data=str(ADDING_SELF)),
        ],
        [
            InlineKeyboardButton(text="Show data", callback_data=str(SHOWING)),
            InlineKeyboardButton(text="Done", callback_data=str(END)),
        ],
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    # If we're starting over we don't need to send a new message
    if context.user_data.get(START_OVER):
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
    else:
        await update.message.reply_text(
            "Hi, I'm Family Bot and I'm here to help you gather information about your family."
        )
        await update.message.reply_text(text=text, reply_markup=keyboard)

    context.user_data[START_OVER] = False
    return SELECTING_ACTION

async def adding_self(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Add information about yourself."""
    context.user_data[CURRENT_LEVEL] = SELF
    text = "Okay, please tell me about yourself."
    button = InlineKeyboardButton(text="Add info", callback_data=str(MALE))
    keyboard = InlineKeyboardMarkup.from_button(button)

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)

    return DESCRIBING_SELF

async def show_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Pretty print gathered data."""

    def pretty_print(data: dict[str, Any], level: str) -> str:
        people = data.get(level)
        if not people:
            return " \n No information yet."

        return_str = ""
        if level == SELF:
            for person in data[level]:
                return_str += f" \n Name: {person.get(NAME, '-')} , Age: {person.get(AGE, '-')} "
        else:
            male, female = _name_switcher(level)

            for person in data[level]:
                gender = female if person[GENDER] == FEMALE else male
                return_str += (
                    f" \n {gender} : Name: {person.get(NAME, '-')} , Age: {person.get(AGE, '-')} "
                )
        return return_str

    user_data = context.user_data
    text = f"Yourself: {pretty_print(user_data, SELF)} "
    text += f" \n\n Parents: {pretty_print(user_data, PARENTS)} "
    text += f" \n\n Children: {pretty_print(user_data, CHILDREN)} "

    buttons = [[InlineKeyboardButton(text="Back", callback_data=str(END))]]
    keyboard = InlineKeyboardMarkup(buttons)

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
    user_data[START_OVER] = True

    return SHOWING

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End Conversation by command."""
    await update.message.reply_text("Okay, bye.")

    return END

async def end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End conversation from InlineKeyboardButton."""
    await update.callback_query.answer()

    text = "See you around!"
    await update.callback_query.edit_message_text(text=text)

    return END

# Second level conversation callbacks
async def select_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Choose to add a parent or a child."""
    text = "You may add a parent or a child. Also you can show the gathered data or go back."
    buttons = [
        [
            InlineKeyboardButton(text="Add parent", callback_data=str(PARENTS)),
            InlineKeyboardButton(text="Add child", callback_data=str(CHILDREN)),
        ],
        [
            InlineKeyboardButton(text="Show data", callback_data=str(SHOWING)),
            InlineKeyboardButton(text="Back", callback_data=str(END)),
        ],
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)

    return SELECTING_LEVEL

async def select_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Choose to add mother or father."""
    level = update.callback_query.data
    context.user_data[CURRENT_LEVEL] = level

    text = "Please choose, whom to add."

    male, female = _name_switcher(level)

    buttons = [
        [
            InlineKeyboardButton(text=f"Add {male}", callback_data=str(MALE)),
            InlineKeyboardButton(text=f"Add {female}", callback_data=str(FEMALE)),
        ],
        [
            InlineKeyboardButton(text="Show data", callback_data=str(SHOWING)),
            InlineKeyboardButton(text="Back", callback_data=str(END)),
        ],
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)

    return SELECTING_GENDER

async def end_second_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to top level conversation."""
    context.user_data[START_OVER] = True
    await start(update, context)

    return END

# Third level callbacks
async def select_feature(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Select a feature to update for the person."""
    buttons = [
        [
            InlineKeyboardButton(text="Name", callback_data=str(NAME)),
            InlineKeyboardButton(text="Age", callback_data=str(AGE)),
            InlineKeyboardButton(text="Done", callback_data=str(END)),
        ]
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    # If we collect features for a new person, clear the cache and save the gender
    if not context.user_data.get(START_OVER):
        context.user_data[FEATURES] = {GENDER: update.callback_query.data}
        text = "Please select a feature to update."

        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
    # But after we do that, we need to send a new message
    else:
        text = "Got it! Please select a feature to update."
        await update.message.reply_text(text=text, reply_markup=keyboard)

    context.user_data[START_OVER] = False
    return SELECTING_FEATURE

async def ask_for_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Prompt user to input data for selected feature."""
    context.user_data[CURRENT_FEATURE] = update.callback_query.data
    text = "Okay, tell me."

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text=text)

    return TYPING

async def save_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Save input for feature and return to feature selection."""
    user_data = context.user_data
    user_data[FEATURES][user_data[CURRENT_FEATURE]] = update.message.text

    user_data[START_OVER] = True

    return await select_feature(update, context)

async def end_describing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End gathering of features and return to parent conversation."""
    user_data = context.user_data
    level = user_data[CURRENT_LEVEL]
    if not user_data.get(level):
        user_data[level] = []
    user_data[level].append(user_data[FEATURES])

    # Print upper level menu
    if level == SELF:
        user_data[START_OVER] = True
        await start(update, context)
    else:
        await select_level(update, context)

    return END

async def stop_nested(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Completely end conversation from within nested conversation."""
    await update.message.reply_text("Okay, bye.")

    return STOPPING

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # Set up third level ConversationHandler (collecting features)
    description_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                select_feature, pattern="^" + str(MALE) + "$|^" + str(FEMALE) + "$"
            )
        ],
        states={
            SELECTING_FEATURE: [
                CallbackQueryHandler(ask_for_input, pattern="^(?!" + str(END) + ").*$")
            ],
            TYPING: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_input)],
        },
        fallbacks=[
            CallbackQueryHandler(end_describing, pattern="^" + str(END) + "$"),
            CommandHandler("stop", stop_nested),
        ],
        map_to_parent={
            # Return to second level menu
            END: SELECTING_LEVEL,
            # End conversation altogether
            STOPPING: STOPPING,
        },
    )

    # Set up second level ConversationHandler (adding a person)
    add_member_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(select_level, pattern="^" + str(ADDING_MEMBER) + "$")],
        states={
            SELECTING_LEVEL: [
                CallbackQueryHandler(select_gender, pattern=f"^{PARENTS}$|^{CHILDREN}$")
            ],
            SELECTING_GENDER: [description_conv],
        },
        fallbacks=[
            CallbackQueryHandler(show_data, pattern="^" + str(SHOWING) + "$"),
            CallbackQueryHandler(end_second_level, pattern="^" + str(END) + "$"),
            CommandHandler("stop", stop_nested),
        ],
        map_to_parent={
            # After showing data return to top level menu
            SHOWING: SHOWING,
            # Return to top level menu
            END: SELECTING_ACTION,
            # End conversation altogether
            STOPPING: END,
        },
    )

    # Set up top level ConversationHandler (selecting action)
    # Because the states of the third level conversation map to the ones of the second level
    # conversation, we need to make sure the top level conversation can also handle them
    selection_handlers = [
        add_member_conv,
        CallbackQueryHandler(show_data, pattern="^" + str(SHOWING) + "$"),
        CallbackQueryHandler(adding_self, pattern="^" + str(ADDING_SELF) + "$"),
        CallbackQueryHandler(end, pattern="^" + str(END) + "$"),
    ]
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SHOWING: [CallbackQueryHandler(start, pattern="^" + str(END) + "$")],
            SELECTING_ACTION: selection_handlers, # type: ignore[dict-item]
            SELECTING_LEVEL: selection_handlers, # type: ignore[dict-item]
            DESCRIBING_SELF: [description_conv],
            STOPPING: [CommandHandler("start", start)],
        },
        fallbacks=[CommandHandler("stop", stop)],
    )

    application.add_handler(conv_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

## State Diagram

(State Diagram describing the conversation flow follows)

---

---

# passportbot.py

This example is available for different web frameworks.
You can select your preferred framework by opening one of the tabs above the code example.

Hint

The following examples show how different Python web frameworks can be used alongside PTB.
This can be useful for two use cases:

1. For extending the functionality of your existing bot to handling updates of external services
2. For extending the functionality of your exisiting web application to also include chat bot functionality

How the PTB and web framework components of the examples below are viewed surely depends on which use case one has in mind.
We are fully aware that a combination of PTB with web frameworks will always mean finding a tradeoff between usability and best practices for both PTB and the web framework and these examples are certainly far from optimal solutions.
Please understand them as starting points and use your expertise of the web framework of your choosing to build up on them.
You are of course also very welcome to help improve these examples!

starlette

```python
#!/usr/bin/env python

# This program is dedicated to the public domain under the CC0 license.
# pylint: disable=import-error,unused-argument
"""
Simple example of a bot that uses a custom webhook setup and handles custom updates.
For the custom webhook setup, the libraries `starlette` and `uvicorn` are used. Please install
them as `pip install starlette~=0.20.0 uvicorn~=0.23.2`.
Note that any other `asyncio` based web server framework can be used for a custom webhook setup
just as well.

Usage:
Set bot Token, URL, admin CHAT_ID and PORT after the imports.
You may also need to change the `listen` value in the uvicorn configuration to match your setup.
Press Ctrl-C on the command line or send a signal to the process to stop the bot.
"""
import asyncio
import html
import logging
from dataclasses import dataclass
from http import HTTPStatus

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define configuration constants
URL = "https://domain.tld"
ADMIN_CHAT_ID = 123456
PORT = 8000
TOKEN = "123:ABC" # nosec B105

@dataclass
class WebhookUpdate:
    """Simple dataclass to wrap a custom update type"""

    user_id: int
    payload: str

class CustomContext(CallbackContext[ExtBot, dict, dict, dict]):
    """
    Custom CallbackContext class that makes `user_data` available for updates of type
    `WebhookUpdate`.
    """

    @classmethod
    def from_update(
        cls,
        update: object,
        application: "Application",
    ) -> "CustomContext":
        if isinstance(update, WebhookUpdate):
            return cls(application=application, user_id=update.user_id)
        return super().from_update(update, application)

async def start(update: Update, context: CustomContext) -> None:
    """Display a message with instructions on how to use this bot."""
    payload_url = html.escape(f" {URL} /submitpayload?user_id=<your user id>&payload=<payload>")
    text = (
        f"To check if the bot is still running, call <code> {URL} /healthcheck</code>. \n\n "
        f"To post a custom update, call <code> {payload_url} </code>."
    )
    await update.message.reply_html(text=text)

async def webhook_update(update: WebhookUpdate, context: CustomContext) -> None:
    """Handle custom updates."""
    chat_member = await context.bot.get_chat_member(chat_id=update.user_id, user_id=update.user_id)
    payloads = context.user_data.setdefault("payloads", [])
    payloads.append(update.payload)
    combined_payloads = "</code> \n • <code>".join(payloads)
    text = (
        f"The user {chat_member.user.mention_html()} has sent a new payload. "
        f"So far they have sent the following payloads: \n\n • <code> {combined_payloads} </code>"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)

async def main() -> None:
    """Set up PTB application and a web application for handling the incoming requests."""
    context_types = ContextTypes(context=CustomContext)
    # Here we set updater to None because we want our custom webhook server to handle the updates
    # and hence we don't need an Updater instance
    application = (
        Application.builder().token(TOKEN).updater(None).context_types(context_types).build()
    )

    # register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(TypeHandler(type=WebhookUpdate, callback=webhook_update))

    # Pass webhook settings to telegram
    await application.bot.set_webhook(url=f" {URL} /telegram", allowed_updates=Update.ALL_TYPES)

    # Set up webserver
    async def telegram(request: Request) -> Response:
        """Handle incoming Telegram updates by putting them into the `update_queue`"""
        await application.update_queue.put(
            Update.de_json(data=await request.json(), bot=application.bot)
        )
        return Response()

    async def custom_updates(request: Request) -> PlainTextResponse:
        """
        Handle incoming webhook updates by also putting them into the `update_queue` if
        the required parameters were passed correctly.
        """
        try:
            user_id = int(request.query_params["user_id"])
            payload = request.query_params["payload"]
        except KeyError:
            return PlainTextResponse(
                status_code=HTTPStatus.BAD_REQUEST,
                content="Please pass both `user_id` and `payload` as query parameters.",
            )
        except ValueError:
            return PlainTextResponse(
                status_code=HTTPStatus.BAD_REQUEST,
                content="The `user_id` must be a string!",
            )

        await application.update_queue.put(WebhookUpdate(user_id=user_id, payload=payload))
        return PlainTextResponse("Thank you for the submission! It's being forwarded.")

    async def health(_: Request) -> PlainTextResponse:
        """For the health endpoint, reply with a simple plain text message."""
        return PlainTextResponse(content="The bot is still running fine :)")

    starlette_app = Starlette(
        routes=[
            Route("/telegram", telegram, methods=["POST"]),
            Route("/healthcheck", health, methods=["GET"]),
            Route("/submitpayload", custom_updates, methods=["POST", "GET"]),
        ]
    )
    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=starlette_app,
            port=PORT,
            use_colors=False,
            host="127.0.0.1",
        )
    )

    # Run application and webserver together
    async with application:
        await application.start()
        await webserver.serve()
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())

```
flask

```python
#!/usr/bin/env python

# This program is dedicated to the public domain under the CC0 license.
# pylint: disable=import-error,unused-argument
"""
Simple example of a bot that uses a custom webhook setup and handles custom updates.
For the custom webhook setup, the libraries `flask`, `asgiref` and `uvicorn` are used. Please
install them as `pip install flask[async]~=2.3.2 uvicorn~=0.23.2 asgiref~=3.7.2`.
Note that any other `asyncio` based web server framework can be used for a custom webhook setup
just as well.

Usage:
Set bot Token, URL, admin CHAT_ID and PORT after the imports.
You may also need to change the `listen` value in the uvicorn configuration to match your setup.
Press Ctrl-C on the command line or send a signal to the process to stop the bot.
"""
import asyncio
import html
import logging
from dataclasses import dataclass
from http import HTTPStatus

import uvicorn
from asgiref.wsgi import WsgiToAsgi
from flask import Flask, Response, abort, make_response, request

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define configuration constants
URL = "https://domain.tld"
ADMIN_CHAT_ID = 123456
PORT = 8000
TOKEN = "123:ABC" # nosec B105

@dataclass
class WebhookUpdate:
    """Simple dataclass to wrap a custom update type"""

    user_id: int
    payload: str

class CustomContext(CallbackContext[ExtBot, dict, dict, dict]):
    """
    Custom CallbackContext class that makes `user_data` available for updates of type
    `WebhookUpdate`.
    """

    @classmethod
    def from_update(
        cls,
        update: object,
        application: "Application",
    ) -> "CustomContext":
        if isinstance(update, WebhookUpdate):
            return cls(application=application, user_id=update.user_id)
        return super().from_update(update, application)

async def start(update: Update, context: CustomContext) -> None:
    """Display a message with instructions on how to use this bot."""
    payload_url = html.escape(f" {URL} /submitpayload?user_id=<your user id>&payload=<payload>")
    text = (
        f"To check if the bot is still running, call <code> {URL} /healthcheck</code>. \n\n "
        f"To post a custom update, call <code> {payload_url} </code>."
    )
    await update.message.reply_html(text=text)

async def webhook_update(update: WebhookUpdate, context: CustomContext) -> None:
    """Handle custom updates."""
    chat_member = await context.bot.get_chat_member(chat_id=update.user_id, user_id=update.user_id)
    payloads = context.user_data.setdefault("payloads", [])
    payloads.append(update.payload)
    combined_payloads = "</code> \n • <code>".join(payloads)
    text = (
        f"The user {chat_member.user.mention_html()} has sent a new payload. "
        f"So far they have sent the following payloads: \n\n • <code> {combined_payloads} </code>"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)

async def main() -> None:
    """Set up PTB application and a web application for handling the incoming requests."""
    context_types = ContextTypes(context=CustomContext)
    # Here we set updater to None because we want our custom webhook server to handle the updates
    # and hence we don't need an Updater instance
    application = (
        Application.builder().token(TOKEN).updater(None).context_types(context_types).build()
    )

    # register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(TypeHandler(type=WebhookUpdate, callback=webhook_update))

    # Pass webhook settings to telegram
    await application.bot.set_webhook(url=f" {URL} /telegram", allowed_updates=Update.ALL_TYPES)

    # Set up webserver
    flask_app = Flask(__name__)

    @flask_app.post("/telegram") # type: ignore[misc]
    async def telegram() -> Response:
        """Handle incoming Telegram updates by putting them into the `update_queue`"""
        await application.update_queue.put(Update.de_json(data=request.json, bot=application.bot))
        return Response(status=HTTPStatus.OK)

    @flask_app.route("/submitpayload", methods=["GET", "POST"]) # type: ignore[misc]
    async def custom_updates() -> Response:
        """
        Handle incoming webhook updates by also putting them into the `update_queue` if
        the required parameters were passed correctly.
        """
        try:
            user_id = int(request.args["user_id"])
            payload = request.args["payload"]
        except KeyError:
            abort(
                HTTPStatus.BAD_REQUEST,
                "Please pass both `user_id` and `payload` as query parameters.",
            )
        except ValueError:
            abort(HTTPStatus.BAD_REQUEST, "The `user_id` must be a string!")

        await application.update_queue.put(WebhookUpdate(user_id=user_id, payload=payload))
        return Response(status=HTTPStatus.OK)

    @flask_app.get("/healthcheck") # type: ignore[misc]
    async def health() -> Response:
        """For the health endpoint, reply with a simple plain text message."""
        response = make_response("The bot is still running fine :)", HTTPStatus.OK)
        response.mimetype = "text/plain"
        return response

    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=WsgiToAsgi(flask_app),
            port=PORT,
            use_colors=False,
            host="127.0.0.1",
        )
    )

    # Run application and webserver together
    async with application:
        await application.start()
        await webserver.serve()
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())

```
quart

```python
#!/usr/bin/env python

# This program is dedicated to the public domain under the CC0 license.
# pylint: disable=import-error,unused-argument
"""
Simple example of a bot that uses a custom webhook setup and handles custom updates.
For the custom webhook setup, the libraries `quart` and `uvicorn` are used. Please
install them as `pip install quart~=0.18.4 uvicorn~=0.23.2`.
Note that any other `asyncio` based web server framework can be used for a custom webhook setup
just as well.

Usage:
Set bot Token, URL, admin CHAT_ID and PORT after the imports.
You may also need to change the `listen` value in the uvicorn configuration to match your setup.
Press Ctrl-C on the command line or send a signal to the process to stop the bot.
"""
import asyncio
import html
import logging
from dataclasses import dataclass
from http import HTTPStatus

import uvicorn
from quart import Quart, Response, abort, make_response, request

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define configuration constants
URL = "https://domain.tld"
ADMIN_CHAT_ID = 123456
PORT = 8000
TOKEN = "123:ABC" # nosec B105

@dataclass
class WebhookUpdate:
    """Simple dataclass to wrap a custom update type"""

    user_id: int
    payload: str

class CustomContext(CallbackContext[ExtBot, dict, dict, dict]):
    """
    Custom CallbackContext class that makes `user_data` available for updates of type
    `WebhookUpdate`.
    """

    @classmethod
    def from_update(
        cls,
        update: object,
        application: "Application",
    ) -> "CustomContext":
        if isinstance(update, WebhookUpdate):
            return cls(application=application, user_id=update.user_id)
        return super().from_update(update, application)

async def start(update: Update, context: CustomContext) -> None:
    """Display a message with instructions on how to use this bot."""
    payload_url = html.escape(f" {URL} /submitpayload?user_id=<your user id>&payload=<payload>")
    text = (
        f"To check if the bot is still running, call <code> {URL} /healthcheck</code>. \n\n "
        f"To post a custom update, call <code> {payload_url} </code>."
    )
    await update.message.reply_html(text=text)

async def webhook_update(update: WebhookUpdate, context: CustomContext) -> None:
    """Handle custom updates."""
    chat_member = await context.bot.get_chat_member(chat_id=update.user_id, user_id=update.user_id)
    payloads = context.user_data.setdefault("payloads", [])
    payloads.append(update.payload)
    combined_payloads = "</code> \n • <code>".join(payloads)
    text = (
        f"The user {chat_member.user.mention_html()} has sent a new payload. "
        f"So far they have sent the following payloads: \n\n • <code> {combined_payloads} </code>"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)

async def main() -> None:
    """Set up PTB application and a web application for handling the incoming requests."""
    context_types = ContextTypes(context=CustomContext)
    # Here we set updater to None because we want our custom webhook server to handle the updates
    # and hence we don't need an Updater instance
    application = (
        Application.builder().token(TOKEN).updater(None).context_types(context_types).build()
    )

    # register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(TypeHandler(type=WebhookUpdate, callback=webhook_update))

    # Pass webhook settings to telegram
    await application.bot.set_webhook(url=f" {URL} /telegram", allowed_updates=Update.ALL_TYPES)

    # Set up webserver
    quart_app = Quart(__name__)

    @quart_app.post("/telegram") # type: ignore[misc]
    async def telegram() -> Response:
        """Handle incoming Telegram updates by putting them into the `update_queue`"""
        await application.update_queue.put(
            Update.de_json(data=await request.get_json(), bot=application.bot)
        )
        return Response(status=HTTPStatus.OK)

    @quart_app.route("/submitpayload", methods=["GET", "POST"]) # type: ignore[misc]
    async def custom_updates() -> Response:
        """
        Handle incoming webhook updates by also putting them into the `update_queue` if
        the required parameters were passed correctly.
        """
        try:
            user_id = int(request.args["user_id"])
            payload = request.args["payload"]
        except KeyError:
            abort(
                HTTPStatus.BAD_REQUEST,
                "Please pass both `user_id` and `payload` as query parameters.",
            )
        except ValueError:
            abort(HTTPStatus.BAD_REQUEST, "The `user_id` must be a string!")

        await application.update_queue.put(WebhookUpdate(user_id=user_id, payload=payload))
        return Response(status=HTTPStatus.OK)

    @quart_app.get("/healthcheck") # type: ignore[misc]
    async def health() -> Response:
        """For the health endpoint, reply with a simple plain text message."""
        response = await make_response("The bot is still running fine :)", HTTPStatus.OK)
        response.mimetype = "text/plain"
        return response

    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=quart_app,
            port=PORT,
            use_colors=False,
            host="127.0.0.1",
        )
    )

    # Run application and webserver together
    async with application:
        await application.start()
        await webserver.serve()
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())

```
Django

```python
#!/usr/bin/env python

# This program is dedicated to the public domain under the CC0 license.
# pylint: disable=import-error,unused-argument
"""
Simple example of a bot that uses a custom webhook setup and handles custom updates.
For the custom webhook setup, the libraries `Django` and `uvicorn` are used. Please
install them as `pip install Django~=4.2.4 uvicorn~=0.23.2`.
Note that any other `asyncio` based web server framework can be used for a custom webhook setup
just as well.

Usage:
Set bot Token, URL, admin CHAT_ID and PORT after the imports.
You may also need to change the `listen` value in the uvicorn configuration to match your setup.
Press Ctrl-C on the command line or send a signal to the process to stop the bot.
"""
import asyncio
import html
import json
import logging
from dataclasses import dataclass
from uuid import uuid4

import uvicorn
from django.conf import settings
from django.core.asgi import get_asgi_application
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.urls import path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define configuration constants
URL = "https://domain.tld"
ADMIN_CHAT_ID = 123456
PORT = 8000
TOKEN = "123:ABC" # nosec B105

@dataclass
class WebhookUpdate:
    """Simple dataclass to wrap a custom update type"""

    user_id: int
    payload: str

class CustomContext(CallbackContext[ExtBot, dict, dict, dict]):
    """
    Custom CallbackContext class that makes `user_data` available for updates of type
    `WebhookUpdate`.
    """

    @classmethod
    def from_update(
        cls,
        update: object,
        application: "Application",
    ) -> "CustomContext":
        if isinstance(update, WebhookUpdate):
            return cls(application=application, user_id=update.user_id)
        return super().from_update(update, application)

async def start(update: Update, context: CustomContext) -> None:
    """Display a message with instructions on how to use this bot."""
    payload_url = html.escape(f" {URL} /submitpayload?user_id=<your user id>&payload=<payload>")
    text = (
        f"To check if the bot is still running, call <code> {URL} /healthcheck</code>. \n\n "
        f"To post a custom update, call <code> {payload_url} </code>."
    )
    await update.message.reply_html(text=text)

async def webhook_update(update: WebhookUpdate, context: CustomContext) -> None:
    """Handle custom updates."""
    chat_member = await context.bot.get_chat_member(chat_id=update.user_id, user_id=update.user_id)
    payloads = context.user_data.setdefault("payloads", [])
    payloads.append(update.payload)
    combined_payloads = "</code> \n • <code>".join(payloads)
    text = (
        f"The user {chat_member.user.mention_html()} has sent a new payload. "
        f"So far they have sent the following payloads: \n\n • <code> {combined_payloads} </code>"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)

async def telegram(request: HttpRequest) -> HttpResponse:
    """Handle incoming Telegram updates by putting them into the `update_queue`"""
    await ptb_application.update_queue.put(
        Update.de_json(data=json.loads(request.body), bot=ptb_application.bot)
    )
    return HttpResponse()

async def custom_updates(request: HttpRequest) -> HttpResponse:
    """
    Handle incoming webhook updates by also putting them into the `update_queue` if
    the required parameters were passed correctly.
    """
    try:
        user_id = int(request.GET["user_id"])
        payload = request.GET["payload"]
    except KeyError:
        return HttpResponseBadRequest(
            "Please pass both `user_id` and `payload` as query parameters.",
        )
    except ValueError:
        return HttpResponseBadRequest("The `user_id` must be a string!")

    await ptb_application.update_queue.put(WebhookUpdate(user_id=user_id, payload=payload))
    return HttpResponse()

async def health(_: HttpRequest) -> HttpResponse:
    """For the health endpoint, reply with a simple plain text message."""
    return HttpResponse("The bot is still running fine :)")

# Set up PTB application and a web application for handling the incoming requests.

context_types = ContextTypes(context=CustomContext)

# Here we set updater to None because we want our custom webhook server to handle the updates
# and hence we don't need an Updater instance
ptb_application = (
    Application.builder().token(TOKEN).updater(None).context_types(context_types).build()
)

# register handlers
ptb_application.add_handler(CommandHandler("start", start))
ptb_application.add_handler(TypeHandler(type=WebhookUpdate, callback=webhook_update))

urlpatterns = [
    path("telegram", telegram, name="Telegram updates"),
    path("submitpayload", custom_updates, name="custom updates"),
    path("healthcheck", health, name="health check"),
]
settings.configure(ROOT_URLCONF=__name__, SECRET_KEY=uuid4().hex)

async def main() -> None:
    """Finalize configuration and run the applications."""
    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=get_asgi_application(),
            port=PORT,
            use_colors=False,
            host="127.0.0.1",
        )
    )

    # Pass webhook settings to telegram
    await ptb_application.bot.set_webhook(url=f" {URL} /telegram", allowed_updates=Update.ALL_TYPES)

    # Run application and webserver together
    async with ptb_application:
        await ptb_application.start()
        await webserver.serve()
        await ptb_application.stop()

if __name__ == "__main__":
    asyncio.run(main())

```

---

---

# deeplinking.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""Bot that explains Telegram's "Deep Linking Parameters" functionality.

This program is dedicated to the public domain under the CC0 license.

This Bot uses the Application class to handle the bot.

First, a few handler functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Deep Linking example. Send /start to get the link.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
    helpers,
)
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define constants that will allow us to reuse the deep-linking parameters.
CHECK_THIS_OUT = "check-this-out"
USING_ENTITIES = "using-entities-here"
USING_KEYBOARD = "using-keyboard-here"
SO_COOL = "so-cool"

# Callback data to pass in 3rd level deep-linking
KEYBOARD_CALLBACKDATA = "keyboard-callback-data"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a deep-linked URL when the command /start is issued."""
    bot = context.bot
    url = helpers.create_deep_linked_url(bot.username, CHECK_THIS_OUT, group=True)
    text = "Feel free to tell your friends about it: \n\n " + url
    await update.message.reply_text(text)

async def deep_linked_level_1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reached through the CHECK_THIS_OUT payload"""
    bot = context.bot
    url = helpers.create_deep_linked_url(bot.username, SO_COOL)
    text = (
        "Awesome, you just accessed hidden functionality! Now let's get back to the private chat."
    )
    keyboard = InlineKeyboardMarkup.from_button(
        InlineKeyboardButton(text="Continue here!", url=url)
    )
    await update.message.reply_text(text, reply_markup=keyboard)

async def deep_linked_level_2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reached through the SO_COOL payload"""
    bot = context.bot
    url = helpers.create_deep_linked_url(bot.username, USING_ENTITIES)
    text = f'You can also mask the deep-linked URLs as links: <a href=" {url} ">▶️ CLICK HERE</a>.'
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)
    )

async def deep_linked_level_3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reached through the USING_ENTITIES payload"""
    await update.message.reply_text(
        "It is also possible to make deep-linking using InlineKeyboardButtons.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="Like this!", callback_data=KEYBOARD_CALLBACKDATA)]]
        ),
    )

async def deep_link_level_3_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answers CallbackQuery with deeplinking url."""
    bot = context.bot
    url = helpers.create_deep_linked_url(bot.username, USING_KEYBOARD)
    await update.callback_query.answer(url=url)

async def deep_linked_level_4(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reached through the USING_KEYBOARD payload"""
    payload = context.args
    await update.message.reply_text(
        f"Congratulations! This is as deep as it gets 👏🏻 \n\n The payload was: {payload} "
    )

def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # More info on what deep linking actually is (read this first if it's unclear to you):
    # https://core.telegram.org/bots/features#deep-linking

    # Register a deep-linking handler
    application.add_handler(
        CommandHandler("start", deep_linked_level_1, filters.Regex(CHECK_THIS_OUT))
    )

    # This one works with a textual link instead of an URL
    application.add_handler(CommandHandler("start", deep_linked_level_2, filters.Regex(SO_COOL)))

    # We can also pass on the deep-linking payload
    application.add_handler(
        CommandHandler("start", deep_linked_level_3, filters.Regex(USING_ENTITIES))
    )

    # Possible with inline keyboard buttons as well
    application.add_handler(
        CommandHandler("start", deep_linked_level_4, filters.Regex(USING_KEYBOARD))
    )

    # register callback handler for inline keyboard button
    application.add_handler(
        CallbackQueryHandler(deep_link_level_3_callback, pattern=KEYBOARD_CALLBACKDATA)
    )

    # Make sure the deep-linking handlers occur *before*the normal /start handler.
    application.add_handler(CommandHandler("start", start))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# echobot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Simple Bot to reply to Telegram messages.

First, a few handler functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Basic Echobot example, repeats messages.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging

from telegram import ForceReply, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define a few command handlers. These usually take the two arguments update and
# context.
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}!",
        reply_markup=ForceReply(selective=True),
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text("Help!")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Echo the user message."""
    await update.message.reply_text(update.message.text)

def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # on non command i.e message - echo the message on Telegram
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# errorhandlerbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""This is a very simple example on how one could implement a custom error handler."""
import html
import json
import logging
import traceback

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# This can be your own ID, or one for a developer group/channel.
# You can use the /start command of this bot to see your chat id.
DEVELOPER_CHAT_ID = 123456789

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    # Log the error before we do anything else, so we can see it even if something breaks.
    logger.error("Exception while handling an update:", exc_info=context.error)

    # traceback.format_exception returns the usual python message about an exception, but as a
    # list of strings rather than a single string, so we have to join them together.
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    # Build the message with some markup and additional information about what happened.
    # You might need to add some logic to deal with messages longer than the 4096 character limit.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    message = (
        "An exception was raised while handling an update \n "
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
        "</pre> \n\n "
        f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre> \n\n "
        f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre> \n\n "
        f"<pre> {html.escape(tb_string)}</pre>"
    )

    # Finally, send the message
    await context.bot.send_message(
        chat_id=DEVELOPER_CHAT_ID, text=message, parse_mode=ParseMode.HTML
    )

async def bad_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Raise an error to trigger the error handler."""
    await context.bot.wrong_method_name() # type: ignore[attr-defined]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays info on how to trigger an error."""
    await update.effective_message.reply_html(
        "Use /bad_command to cause an error. \n "
        f"Your chat id is <code> {update.effective_chat.id} </code>."
    )

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # Register the commands...
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("bad_command", bad_command))

    # ...and the error handler
    application.add_error_handler(error_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# inlinebot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Don't forget to enable inline mode with @BotFather

First, a few handler functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Basic inline bot example. Applies different text transformations.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""
import logging
from html import escape
from uuid import uuid4

from telegram import InlineQueryResultArticle, InputTextMessageContent, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, InlineQueryHandler

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define a few command handlers. These usually take the two arguments update and
# context.
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    await update.message.reply_text("Hi!")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_text("Help!")

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the inline query. This is run when you type: @botusername <query>"""
    query = update.inline_query.query

    if not query: # empty query should not be handled
        return

    results = [
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="Caps",
            input_message_content=InputTextMessageContent(query.upper()),
        ),
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="Bold",
            input_message_content=InputTextMessageContent(
                f"<b> {escape(query)} </b>", parse_mode=ParseMode.HTML
            ),
        ),
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="Italic",
            input_message_content=InputTextMessageContent(
                f"<i> {escape(query)} </i>", parse_mode=ParseMode.HTML
            ),
        ),
    ]

    await update.inline_query.answer(results)

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    # on inline queries - show corresponding inline results
    application.add_handler(InlineQueryHandler(inline_query))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# inlinekeyboard2.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""Simple inline keyboard bot with multiple CallbackQueryHandlers.

This Bot uses the Application class to handle the bot.
First, a few callback functions are defined as callback query handler. Then, those functions are
passed to the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.
Usage:
Example of a bot that uses inline keyboard that has multiple CallbackQueryHandlers arranged in a
ConversationHandler.
Send /start to initiate the conversation.
Press Ctrl-C on the command line to stop the bot.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Stages
START_ROUTES, END_ROUTES = range(2)

# Callback data
ONE, TWO, THREE, FOUR = range(4)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send message on `/start`."""
    # Get user that sent /start and log his name
    user = update.message.from_user
    logger.info("User %s started the conversation.", user.first_name)
    # Build InlineKeyboard where each button has a displayed text
    # and a string as callback_data
    # The keyboard is a list of button rows, where each row is in turn
    # a list (hence `[[...]]`).
    keyboard = [
        [
            InlineKeyboardButton("1", callback_data=str(ONE)),
            InlineKeyboardButton("2", callback_data=str(TWO)),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # Send message with text and appended InlineKeyboard
    await update.message.reply_text("Start handler, Choose a route", reply_markup=reply_markup)
    # Tell ConversationHandler that we're in state `FIRST` now
    return START_ROUTES

async def start_over(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt same text & keyboard as `start` does but not as new message"""
    # Get CallbackQuery from Update
    query = update.callback_query
    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("1", callback_data=str(ONE)),
            InlineKeyboardButton("2", callback_data=str(TWO)),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # Instead of sending a new message, edit the message that
    # originated the CallbackQuery. This gives the feeling of an
    # interactive menu.
    await query.edit_message_text(text="Start handler, Choose a route", reply_markup=reply_markup)
    return START_ROUTES

async def one(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show new choice of buttons"""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("3", callback_data=str(THREE)),
            InlineKeyboardButton("4", callback_data=str(FOUR)),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text="First CallbackQueryHandler, Choose a route", reply_markup=reply_markup
    )
    return START_ROUTES

async def two(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show new choice of buttons"""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("1", callback_data=str(ONE)),
            InlineKeyboardButton("3", callback_data=str(THREE)),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text="Second CallbackQueryHandler, Choose a route", reply_markup=reply_markup
    )
    return START_ROUTES

async def three(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show new choice of buttons. This is the end point of the conversation."""
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("Yes, let's do it again!", callback_data=str(ONE)),
            InlineKeyboardButton("Nah, I've had enough ...", callback_data=str(TWO)),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text="Third CallbackQueryHandler. Do want to start over?", reply_markup=reply_markup
    )
    # Transfer to conversation state `SECOND`
    return END_ROUTES

async def end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Returns `ConversationHandler.END`, which tells the
    ConversationHandler that the conversation is over.
    """
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="See you next time!")
    return ConversationHandler.END

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # Setup conversation handler with the states FIRST and SECOND
    # Use the pattern parameter to pass CallbackQueries with specific
    # data pattern to the corresponding handlers.
    # ^ means "start of line/string"
    # $ means "end of line/string"
    # So ^ABC$ will only allow 'ABC'
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            START_ROUTES: [
                CallbackQueryHandler(one, pattern="^" + str(ONE) + "$"),
                CallbackQueryHandler(two, pattern="^" + str(TWO) + "$"),
                CallbackQueryHandler(three, pattern="^" + str(THREE) + "$"),
                CallbackQueryHandler(four, pattern="^" + str(FOUR) + "$"),
            ],
            END_ROUTES: [
                CallbackQueryHandler(start_over, pattern="^" + str(ONE) + "$"),
                CallbackQueryHandler(end, pattern="^" + str(TWO) + "$"),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    # Add ConversationHandler to application that will be used for handling updates
    application.add_handler(conv_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# inlinekeyboard.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Basic example for a bot that uses inline keyboards. For an in-depth explanation, check out
https://github.com/python-telegram-bot/python-telegram-bot/wiki/InlineKeyboard-Example.
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message with three inline buttons attached."""
    keyboard = [
        [
            InlineKeyboardButton("Option 1", callback_data="1"),
            InlineKeyboardButton("Option 2", callback_data="2"),
        ],
        [InlineKeyboardButton("Option 3", callback_data="3")],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("Please choose:", reply_markup=reply_markup)

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query

    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()

    await query.edit_message_text(text=f"Selected option: {query.data}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays info on how to use the bot."""
    await update.message.reply_text("Use /start to test this bot.")

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(CommandHandler("help", help_command))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# nestedconversationbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
First, a few callback functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Example of a bot-user conversation using nested ConversationHandlers.
Send /start to initiate the conversation.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# State definitions for top level conversation
SELECTING_ACTION, ADDING_MEMBER, ADDING_SELF, DESCRIBING_SELF = map(chr, range(4))

# State definitions for second level conversation
SELECTING_LEVEL, SELECTING_GENDER = map(chr, range(4, 6))

# State definitions for descriptions conversation
SELECTING_FEATURE, TYPING = map(chr, range(6, 8))

# Meta states
STOPPING, SHOWING = map(chr, range(8, 10))

# Shortcut for ConversationHandler.END
END = ConversationHandler.END

# Different constants for this example
(
    PARENTS,
    CHILDREN,
    SELF,
    GENDER,
    MALE,
    FEMALE,
    AGE,
    NAME,
    START_OVER,
    FEATURES,
    CURRENT_FEATURE,
    CURRENT_LEVEL,
) = map(chr, range(10, 22))

# Helper
def _name_switcher(level: str) -> tuple[str, str]:
    if level == PARENTS:
        return "Father", "Mother"
    return "Brother", "Sister"

# Top level conversation callbacks
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Select an action: Adding parent/child or show data."""
    text = (
        "You may choose to add a family member, yourself, show the gathered data, or end the "
        "conversation. To abort, simply type /stop."
    )

    buttons = [
        [
            InlineKeyboardButton(text="Add family member", callback_data=str(ADDING_MEMBER)),
            InlineKeyboardButton(text="Add yourself", callback_data=str(ADDING_SELF)),
        ],
        [
            InlineKeyboardButton(text="Show data", callback_data=str(SHOWING)),
            InlineKeyboardButton(text="Done", callback_data=str(END)),
        ],
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    # If we're starting over we don't need to send a new message
    if context.user_data.get(START_OVER):
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
    else:
        await update.message.reply_text(
            "Hi, I'm Family Bot and I'm here to help you gather information about your family."
        )
        await update.message.reply_text(text=text, reply_markup=keyboard)

    context.user_data[START_OVER] = False
    return SELECTING_ACTION

async def adding_self(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Add information about yourself."""
    context.user_data[CURRENT_LEVEL] = SELF
    text = "Okay, please tell me about yourself."
    button = InlineKeyboardButton(text="Add info", callback_data=str(MALE))
    keyboard = InlineKeyboardMarkup.from_button(button)

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)

    return DESCRIBING_SELF

async def show_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Pretty print gathered data."""

    def pretty_print(data: dict[str, Any], level: str) -> str:
        people = data.get(level)
        if not people:
            return " \n No information yet."

        return_str = ""
        if level == SELF:
            for person in data[level]:
                return_str += f" \n Name: {person.get(NAME, '-')} , Age: {person.get(AGE, '-')} "
        else:
            male, female = _name_switcher(level)

            for person in data[level]:
                gender = female if person[GENDER] == FEMALE else male
                return_str += (
                    f" \n {gender} : Name: {person.get(NAME, '-')} , Age: {person.get(AGE, '-')} "
                )
        return return_str

    user_data = context.user_data
    text = f"Yourself: {pretty_print(user_data, SELF)} "
    text += f" \n\n Parents: {pretty_print(user_data, PARENTS)} "
    text += f" \n\n Children: {pretty_print(user_data, CHILDREN)} "

    buttons = [[InlineKeyboardButton(text="Back", callback_data=str(END))]]
    keyboard = InlineKeyboardMarkup(buttons)

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
    user_data[START_OVER] = True

    return SHOWING

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End Conversation by command."""
    await update.message.reply_text("Okay, bye.")

    return END

async def end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End conversation from InlineKeyboardButton."""
    await update.callback_query.answer()

    text = "See you around!"
    await update.callback_query.edit_message_text(text=text)

    return END

# Second level conversation callbacks
async def select_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Choose to add a parent or a child."""
    text = "You may add a parent or a child. Also you can show the gathered data or go back."
    buttons = [
        [
            InlineKeyboardButton(text="Add parent", callback_data=str(PARENTS)),
            InlineKeyboardButton(text="Add child", callback_data=str(CHILDREN)),
        ],
        [
            InlineKeyboardButton(text="Show data", callback_data=str(SHOWING)),
            InlineKeyboardButton(text="Back", callback_data=str(END)),
        ],
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)

    return SELECTING_LEVEL

async def select_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Choose to add mother or father."""
    level = update.callback_query.data
    context.user_data[CURRENT_LEVEL] = level

    text = "Please choose, whom to add."

    male, female = _name_switcher(level)

    buttons = [
        [
            InlineKeyboardButton(text=f"Add {male}", callback_data=str(MALE)),
            InlineKeyboardButton(text=f"Add {female}", callback_data=str(FEMALE)),
        ],
        [
            InlineKeyboardButton(text="Show data", callback_data=str(SHOWING)),
            InlineKeyboardButton(text="Back", callback_data=str(END)),
        ],
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)

    return SELECTING_GENDER

async def end_second_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to top level conversation."""
    context.user_data[START_OVER] = True
    await start(update, context)

    return END

# Third level callbacks
async def select_feature(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Select a feature to update for the person."""
    buttons = [
        [
            InlineKeyboardButton(text="Name", callback_data=str(NAME)),
            InlineKeyboardButton(text="Age", callback_data=str(AGE)),
            InlineKeyboardButton(text="Done", callback_data=str(END)),
        ]
    ]
    keyboard = InlineKeyboardMarkup(buttons)

    # If we collect features for a new person, clear the cache and save the gender
    if not context.user_data.get(START_OVER):
        context.user_data[FEATURES] = {GENDER: update.callback_query.data}
        text = "Please select a feature to update."

        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
    # But after we do that, we need to send a new message
    else:
        text = "Got it! Please select a feature to update."
        await update.message.reply_text(text=text, reply_markup=keyboard)

    context.user_data[START_OVER] = False
    return SELECTING_FEATURE

async def ask_for_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Prompt user to input data for selected feature."""
    context.user_data[CURRENT_FEATURE] = update.callback_query.data
    text = "Okay, tell me."

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text=text)

    return TYPING

async def save_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Save input for feature and return to feature selection."""
    user_data = context.user_data
    user_data[FEATURES][user_data[CURRENT_FEATURE]] = update.message.text

    user_data[START_OVER] = True

    return await select_feature(update, context)

async def end_describing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """End gathering of features and return to parent conversation."""
    user_data = context.user_data
    level = user_data[CURRENT_LEVEL]
    if not user_data.get(level):
        user_data[level] = []
    user_data[level].append(user_data[FEATURES])

    # Print upper level menu
    if level == SELF:
        user_data[START_OVER] = True
        await start(update, context)
    else:
        await select_level(update, context)

    return END

async def stop_nested(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Completely end conversation from within nested conversation."""
    await update.message.reply_text("Okay, bye.")

    return STOPPING

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # Set up third level ConversationHandler (collecting features)
    description_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                select_feature, pattern="^" + str(MALE) + "$|^" + str(FEMALE) + "$"
            )
        ],
        states={
            SELECTING_FEATURE: [
                CallbackQueryHandler(ask_for_input, pattern="^(?!" + str(END) + ").*$")
            ],
            TYPING: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_input)],
        },
        fallbacks=[
            CallbackQueryHandler(end_describing, pattern="^" + str(END) + "$"),
            CommandHandler("stop", stop_nested),
        ],
        map_to_parent={
            # Return to second level menu
            END: SELECTING_LEVEL,
            # End conversation altogether
            STOPPING: STOPPING,
        },
    )

    # Set up second level ConversationHandler (adding a person)
    add_member_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(select_level, pattern="^" + str(ADDING_MEMBER) + "$")],
        states={
            SELECTING_LEVEL: [
                CallbackQueryHandler(select_gender, pattern=f"^{PARENTS}$|^{CHILDREN}$")
            ],
            SELECTING_GENDER: [description_conv],
        },
        fallbacks=[
            CallbackQueryHandler(show_data, pattern="^" + str(SHOWING) + "$"),
            CallbackQueryHandler(end_second_level, pattern="^" + str(END) + "$"),
            CommandHandler("stop", stop_nested),
        ],
        map_to_parent={
            # After showing data return to top level menu
            SHOWING: SHOWING,
            # Return to top level menu
            END: SELECTING_ACTION,
            # End conversation altogether
            STOPPING: END,
        },
    )

    # Set up top level ConversationHandler (selecting action)
    # Because the states of the third level conversation map to the ones of the second level
    # conversation, we need to make sure the top level conversation can also handle them
    selection_handlers = [
        add_member_conv,
        CallbackQueryHandler(show_data, pattern="^" + str(SHOWING) + "$"),
        CallbackQueryHandler(adding_self, pattern="^" + str(ADDING_SELF) + "$"),
        CallbackQueryHandler(end, pattern="^" + str(END) + "$"),
    ]
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            SHOWING: [CallbackQueryHandler(start, pattern="^" + str(END) + "$")],
            SELECTING_ACTION: selection_handlers, # type: ignore[dict-item]
            SELECTING_LEVEL: selection_handlers, # type: ignore[dict-item]
            DESCRIBING_SELF: [description_conv],
            STOPPING: [CommandHandler("start", start)],
        },
        fallbacks=[CommandHandler("stop", stop)],
    )

    application.add_handler(conv_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

## State Diagram

(State Diagram describing the conversation flow follows)

---

---

# passportbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Simple Bot to print/download all incoming passport data

See https://telegram.org/blog/passport for info about what telegram passport is.

See https://github.com/python-telegram-bot/python-telegram-bot/wiki/Telegram-Passport
for how to use Telegram Passport properly with python-telegram-bot.

Note:
To use Telegram Passport, you must install PTB via
`pip install "python-telegram-bot[passport]"`
"""
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

# Enable logging

logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Downloads and prints the received passport data."""
    # Retrieve passport data
    passport_data = update.message.passport_data
    # If our nonce doesn't match what we think, this Update did not originate from us
    # Ideally you would randomize the nonce on the server
    if passport_data.decrypted_credentials.nonce != "thisisatest":
        return

    # Print the decrypted credential data
    # For all elements
    # Print their decrypted data
    # Files will be downloaded to current directory
    for data in passport_data.decrypted_data: # This is where the data gets decrypted
        if data.type == "phone_number":
            logger.info("Phone: %s ", data.phone_number)
        elif data.type == "email":
            logger.info("Email: %s ", data.email)
        if data.type in (
            "personal_details",
            "passport",
            "driver_license",
            "identity_card",
            "internal_passport",
            "address",
        ):
            logger.info(data.type, data.data)
        if data.type in (
            "utility_bill",
            "bank_statement",
            "rental_agreement",
            "passport_registration",
            "temporary_registration",
        ):
            logger.info(data.type, len(data.files), "files")
            for file in data.files:
                actual_file = await file.get_file()
                logger.info(actual_file)
                await actual_file.download_to_drive()
        if (
            data.type in ("passport", "driver_license", "identity_card", "internal_passport")
            and data.front_side
        ):
            front_file = await data.front_side.get_file()
            logger.info(data.type, front_file)
            await front_file.download_to_drive()
        if data.type in ("driver_license" and "identity_card") and data.reverse_side:
            reverse_file = await data.reverse_side.get_file()
            logger.info(data.type, reverse_file)
            await reverse_file.download_to_drive()
        if (
            data.type in ("passport", "driver_license", "identity_card", "internal_passport")
            and data.selfie
        ):
            selfie_file = await data.selfie.get_file()
            logger.info(data.type, selfie_file)
            await selfie_file.download_to_drive()
        if data.translation and data.type in (
            "passport",
            "driver_license",
            "identity_card",
            "internal_passport",
            "utility_bill",
            "bank_statement",
            "rental_agreement",
            "passport_registration",
            "temporary_registration",
        ):
            logger.info(data.type, len(data.translation), "translation")
            for file in data.translation:
                actual_file = await file.get_file()
                logger.info(actual_file)
                await actual_file.download_to_drive()

def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your token and private key
    private_key = Path("private.key")
    application = (
        Application.builder().token("TOKEN").private_key(private_key.read_bytes()).build()
    )

    # On messages that include passport data call msg
    application.add_handler(MessageHandler(filters.PASSPORT_DATA, msg))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

## HTML Page

```html
<!DOCTYPE html>

<html lang="en">
<head>
    <title>Telegram passport test!</title>
    <meta charset="utf-8">
    <meta content="IE=edge" http-equiv="X-UA-Compatible">
    <meta content="width=device-width, initial-scale=1" name="viewport">
</head>
<body>
    <h1>Telegram passport test</h1>

    <div id="telegram_passport_auth"></div>
</body>

<!--- Needs file from https://github.com/TelegramMessenger/TGPassportJsSDK downloaded --->
<script src="telegram-passport.js"></script>
<script>
"use strict";

Telegram.Passport.createAuthButton('telegram_passport_auth', {
    bot_id: 1234567890, // YOUR BOT ID
    scope: {
        data: [{
            type: 'id_document',
            selfie: true
        }, 'address_document', 'phone_number', 'email'], v: 1
    }, // WHAT DATA YOU WANT TO RECEIVE
    public_key: '-----BEGIN PUBLIC KEY-----\n', // YOUR PUBLIC KEY
    nonce: 'thisisatest', // YOUR BOT WILL RECEIVE THIS DATA WITH THE REQUEST
    callback_url: 'https://example.org' // TELEGRAM WILL SEND YOUR USER BACK TO THIS URL
});

</script>
</html>

```

---

---

# paymentbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""Basic example for a bot that can receive payments from users."""

import logging

from telegram import LabeledPrice, ShippingOption, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    ShippingQueryHandler,
    filters,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Insert the token from your payment provider.
# In order to get a provider_token see https://core.telegram.org/bots/payments#getting-a-token
PAYMENT_PROVIDER_TOKEN = "PAYMENT_PROVIDER_TOKEN"

async def start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Provides instructions on how to use the bot."""
    msg = (
        "Use /shipping to receive an invoice with shipping included, or /noshipping for an "
        "invoice without shipping."
    )
    await update.message.reply_text(msg)

async def start_with_shipping_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends an invoice which triggers a shipping query."""
    chat_id = update.message.chat_id
    title = "Payment Example"
    description = "Example of a payment process using the python-telegram-bot library."
    # Unique payload to identify this payment request as being from your bot
    payload = "Custom-Payload"
    # Set up the currency.
    # List of supported currencies: https://core.telegram.org/bots/payments#supported-currencies
    currency = "USD"
    # Price in dollars
    price = 1
    # Convert price to cents from dollars.
    prices = [LabeledPrice("Test", price *100)]
    # Optional parameters like need_shipping_address and is_flexible trigger extra user prompts
    # https://docs.python-telegram-bot.org/en/stable/telegram.bot.html#telegram.Bot.send_invoice
    await context.bot.send_invoice(
        chat_id,
        title,
        description,
        payload,
        currency,
        prices,
        provider_token=PAYMENT_PROVIDER_TOKEN,
        need_name=True,
        need_phone_number=True,
        need_email=True,
        need_shipping_address=True,
        is_flexible=True,
    )

async def start_without_shipping_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Sends an invoice without requiring shipping details."""
    chat_id = update.message.chat_id
    title = "Payment Example"
    description = "Example of a payment process using the python-telegram-bot library."
    # Unique payload to identify this payment request as being from your bot
    payload = "Custom-Payload"
    currency = "USD"
    # Price in dollars
    price = 1
    # Convert price to cents from dollars.
    prices = [LabeledPrice("Test", price *100)]

    # optionally pass need_name=True, need_phone_number=True,
    # need_email=True, need_shipping_address=True, is_flexible=True
    await context.bot.send_invoice(
        chat_id,
        title,
        description,
        payload,
        currency,
        prices,
        provider_token=PAYMENT_PROVIDER_TOKEN,
    )

async def shipping_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the ShippingQuery with available shipping options."""
    query = update.shipping_query
    # Verify if the payload matches, ensure it's from your bot
    if query.invoice_payload != "Custom-Payload":
        # If not, respond with an error
        await query.answer(ok=False, error_message="Something went wrong...")
        return

    # Define available shipping options
    # First option with a single price entry
    options = [ShippingOption("1", "Shipping Option A", [LabeledPrice("A", 100)])]
    # Second option with multiple price entries
    price_list = [LabeledPrice("B1", 150), LabeledPrice("B2", 200)]
    options.append(ShippingOption("2", "Shipping Option B", price_list))
    await query.answer(ok=True, shipping_options=options)

# After (optional) shipping, process the pre-checkout step
async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responds to the PreCheckoutQuery as the final confirmation for checkout."""
    query = update.pre_checkout_query
    # Verify if the payload matches, ensure it's from your bot
    if query.invoice_payload != "Custom-Payload":
        # If not, respond with an error
        await query.answer(ok=False, error_message="Something went wrong...")
    else:
        await query.answer(ok=True)

# Final callback after successful payment
async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Acknowledges successful payment and thanks the user."""
    await update.message.reply_text("Thank you for your payment.")

def main() -> None:
    """Starts the bot and sets up handlers."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # Start command to display usage instructions
    application.add_handler(CommandHandler("start", start_callback))

    # Command handlers for starting the payment process
    application.add_handler(CommandHandler("shipping", start_with_shipping_callback))
    application.add_handler(CommandHandler("noshipping", start_without_shipping_callback))

    # Handler for shipping query (if product requires shipping)
    application.add_handler(ShippingQueryHandler(shipping_callback))

    # Pre-checkout handler for verifying payment details.
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))

    # Handler for successful payment. Notify the user that the payment was successful.
    application.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback)
    )

    # Start polling for updates until interrupted (CTRL+C)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# persistentconversationbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
First, a few callback functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Example of a bot-user conversation using ConversationHandler.
Send /start to initiate the conversation.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

CHOOSING, TYPING_REPLY, TYPING_CHOICE = range(3)

reply_keyboard = [
    ["Age", "Favourite colour"],
    ["Number of siblings", "Something else..."],
    ["Done"],
]
markup = ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)

def facts_to_str(user_data: dict[str, str]) -> str:
    """Helper function for formatting the gathered user info."""
    facts = [f" {key} - {value} " for key, value in user_data.items()]
    return " \n ".join(facts).join([" \n ", " \n "])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation, display any stored data and ask user for input."""
    reply_text = "Hi! My name is Doctor Botter."
    if context.user_data:
        reply_text += (
            f" You already told me your {', '.join(context.user_data.keys())}. Why don't you "
            "tell me something more about yourself? Or change anything I already know."
        )
    else:
        reply_text += (
            " I will hold a more complex conversation with you. Why don't you tell me "
            "something about yourself?"
        )
    await update.message.reply_text(reply_text, reply_markup=markup)

    return CHOOSING

async def regular_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask the user for info about the selected predefined choice."""
    text = update.message.text.lower()
    context.user_data["choice"] = text
    if context.user_data.get(text):
        reply_text = (
            f"Your {text}? I already know the following about that: {context.user_data[text]} "
        )
    else:
        reply_text = f"Your {text}? Yes, I would love to hear about that!"
    await update.message.reply_text(reply_text)

    return TYPING_REPLY

async def custom_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask the user for a description of a custom category."""
    await update.message.reply_text(
        'Alright, please send me the category first, for example "Most impressive skill"'
    )

    return TYPING_CHOICE

async def received_information(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store info provided by user and ask for the next category."""
    text = update.message.text
    category = context.user_data["choice"]
    context.user_data[category] = text.lower()
    del context.user_data["choice"]

    await update.message.reply_text(
        "Neat! Just so you know, this is what you already told me:"
        f" {facts_to_str(context.user_data)} "
        "You can tell me more, or change your opinion on something.",
        reply_markup=markup,
    )

    return CHOOSING

async def show_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the gathered info."""
    await update.message.reply_text(
        f"This is what you already told me: {facts_to_str(context.user_data)} "
    )

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display the gathered info and end the conversation."""
    if "choice" in context.user_data:
        del context.user_data["choice"]

    await update.message.reply_text(
        f"I learned these facts about you: {facts_to_str(context.user_data)} Until next time!",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    persistence = PicklePersistence(filepath="conversationbot")
    application = Application.builder().token("TOKEN").persistence(persistence).build()

    # Add conversation handler with the states CHOOSING, TYPING_CHOICE and TYPING_REPLY
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING: [
                MessageHandler(
                    filters.Regex("^(Age|Favourite colour|Number of siblings)$"), regular_choice
                ),
                MessageHandler(filters.Regex("^Something else...$"), custom_choice),
            ],
            TYPING_CHOICE: [
                MessageHandler(
                    filters.TEXT & ~(filters.COMMAND | filters.Regex("^Done$")), regular_choice
                )
            ],
            TYPING_REPLY: [
                MessageHandler(
                    filters.TEXT & ~(filters.COMMAND | filters.Regex("^Done$")),
                    received_information,
                )
            ],
        },
        fallbacks=[MessageHandler(filters.Regex("^Done$"), done)],
        name="my_conversation",
        persistent=True,
    )

    application.add_handler(conv_handler)

    show_data_handler = CommandHandler("show_data", show_data)
    application.add_handler(show_data_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# pollbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Basic example for a bot that works with polls. Only 3 people are allowed to interact with each
poll/quiz the bot generates. The preview command generates a closed poll/quiz, exactly like the
one the user sends the bot
"""
import logging

from telegram import (
    KeyboardButton,
    KeyboardButtonPollType,
    Poll,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    PollHandler,
    filters,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

TOTAL_VOTER_COUNT = 3

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inform user about what this bot can do"""
    await update.message.reply_text(
        "Please select /poll to get a Poll, /quiz to get a Quiz or /preview"
        " to generate a preview for your poll"
    )

async def poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a predefined poll"""
    questions = ["Good", "Really good", "Fantastic", "Great"]
    message = await context.bot.send_poll(
        update.effective_chat.id,
        "How are you?",
        questions,
        is_anonymous=False,
        allows_multiple_answers=True,
    )
    # Save some info about the poll the bot_data for later use in receive_poll_answer
    payload = {
        message.poll.id: {
            "questions": questions,
            "message_id": message.message_id,
            "chat_id": update.effective_chat.id,
            "answers": 0,
        }
    }
    context.bot_data.update(payload)

async def receive_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Summarize a users poll vote"""
    answer = update.poll_answer
    answered_poll = context.bot_data[answer.poll_id]
    try:
        questions = answered_poll["questions"]
        # this means this poll answer update is from an old poll, we can't do our answering then
    except KeyError:
        return
    selected_options = answer.option_ids
    answer_string = ""
    for question_id in selected_options:
        if question_id != selected_options[-1]:
            answer_string += questions[question_id] + " and "
        else:
            answer_string += questions[question_id]
    await context.bot.send_message(
        answered_poll["chat_id"],
        f" {update.effective_user.mention_html()} feels {answer_string} !",
        parse_mode=ParseMode.HTML,
    )
    answered_poll["answers"] += 1
    # Close poll after three participants voted
    if answered_poll["answers"] == TOTAL_VOTER_COUNT:
        await context.bot.stop_poll(answered_poll["chat_id"], answered_poll["message_id"])

async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a predefined poll"""
    questions = ["1", "2", "4", "20"]
    message = await update.effective_message.reply_poll(
        "How many eggs do you need for a cake?", questions, type=Poll.QUIZ, correct_option_id=2
    )
    # Save some info about the poll the bot_data for later use in receive_quiz_answer
    payload = {
        message.poll.id: {"chat_id": update.effective_chat.id, "message_id": message.message_id}
    }
    context.bot_data.update(payload)

async def receive_quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Close quiz after three participants took it"""
    # the bot can receive closed poll updates we don't care about
    if update.poll.is_closed:
        return
    if update.poll.total_voter_count == TOTAL_VOTER_COUNT:
        try:
            quiz_data = context.bot_data[update.poll.id]
            # this means this poll answer update is from an old poll, we can't stop it then
        except KeyError:
            return
        await context.bot.stop_poll(quiz_data["chat_id"], quiz_data["message_id"])

async def preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask user to create a poll and display a preview of it"""
    # using this without a type lets the user chooses what he wants (quiz or poll)
    button = [[KeyboardButton("Press me!", request_poll=KeyboardButtonPollType())]]
    message = "Press the button to let the bot generate a preview for your poll"
    # using one_time_keyboard to hide the keyboard
    await update.effective_message.reply_text(
        message, reply_markup=ReplyKeyboardMarkup(button, one_time_keyboard=True)
    )

async def receive_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """On receiving polls, reply to it by a closed poll copying the received poll"""
    actual_poll = update.effective_message.poll
    # Only need to set the question and options, since all other parameters don't matter for
    # a closed poll
    await update.effective_message.reply_poll(
        question=actual_poll.question,
        options=[o.text for o in actual_poll.options],
        # with is_closed true, the poll/quiz is immediately closed
        is_closed=True,
        reply_markup=ReplyKeyboardRemove(),
    )

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display a help message"""
    await update.message.reply_text("Use /quiz, /poll or /preview to test this bot.")

def main() -> None:
    """Run bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", poll))
    application.add_handler(CommandHandler("quiz", quiz))
    application.add_handler(CommandHandler("preview", preview))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(MessageHandler(filters.POLL, receive_poll))
    application.add_handler(PollAnswerHandler(receive_poll_answer))
    application.add_handler(PollHandler(receive_quiz_answer))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# persistentconversationbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
First, a few callback functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Example of a bot-user conversation using ConversationHandler.
Send /start to initiate the conversation.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import logging

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

CHOOSING, TYPING_REPLY, TYPING_CHOICE = range(3)

reply_keyboard = [
    ["Age", "Favourite colour"],
    ["Number of siblings", "Something else..."],
    ["Done"],
]
markup = ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True)

def facts_to_str(user_data: dict[str, str]) -> str:
    """Helper function for formatting the gathered user info."""
    facts = [f" {key} - {value} " for key, value in user_data.items()]
    return " \n ".join(facts).join([" \n ", " \n "])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation, display any stored data and ask user for input."""
    reply_text = "Hi! My name is Doctor Botter."
    if context.user_data:
        reply_text += (
            f" You already told me your {', '.join(context.user_data.keys())}. Why don't you "
            "tell me something more about yourself? Or change anything I already know."
        )
    else:
        reply_text += (
            " I will hold a more complex conversation with you. Why don't you tell me "
            "something about yourself?"
        )
    await update.message.reply_text(reply_text, reply_markup=markup)

    return CHOOSING

async def regular_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask the user for info about the selected predefined choice."""
    text = update.message.text.lower()
    context.user_data["choice"] = text
    if context.user_data.get(text):
        reply_text = (
            f"Your {text}? I already know the following about that: {context.user_data[text]} "
        )
    else:
        reply_text = f"Your {text}? Yes, I would love to hear about that!"
    await update.message.reply_text(reply_text)

    return TYPING_REPLY

async def custom_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask the user for a description of a custom category."""
    await update.message.reply_text(
        'Alright, please send me the category first, for example "Most impressive skill"'
    )

    return TYPING_CHOICE

async def received_information(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store info provided by user and ask for the next category."""
    text = update.message.text
    category = context.user_data["choice"]
    context.user_data[category] = text.lower()
    del context.user_data["choice"]

    await update.message.reply_text(
        "Neat! Just so you know, this is what you already told me:"
        f" {facts_to_str(context.user_data)} "
        "You can tell me more, or change your opinion on something.",
        reply_markup=markup,
    )

    return CHOOSING

async def show_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the gathered info."""
    await update.message.reply_text(
        f"This is what you already told me: {facts_to_str(context.user_data)} "
    )

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display the gathered info and end the conversation."""
    if "choice" in context.user_data:
        del context.user_data["choice"]

    await update.message.reply_text(
        f"I learned these facts about you: {facts_to_str(context.user_data)} Until next time!",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END

def main() -> None:
    """Run the bot."""
    # Create the Application and pass it your bot's token.
    persistence = PicklePersistence(filepath="conversationbot")
    application = Application.builder().token("TOKEN").persistence(persistence).build()

    # Add conversation handler with the states CHOOSING, TYPING_CHOICE and TYPING_REPLY
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING: [
                MessageHandler(
                    filters.Regex("^(Age|Favourite colour|Number of siblings)$"), regular_choice
                ),
                MessageHandler(filters.Regex("^Something else...$"), custom_choice),
            ],
            TYPING_CHOICE: [
                MessageHandler(
                    filters.TEXT & ~(filters.COMMAND | filters.Regex("^Done$")), regular_choice
                )
            ],
            TYPING_REPLY: [
                MessageHandler(
                    filters.TEXT & ~(filters.COMMAND | filters.Regex("^Done$")),
                    received_information,
                )
            ],
        },
        fallbacks=[MessageHandler(filters.Regex("^Done$"), done)],
        name="my_conversation",
        persistent=True,
    )

    application.add_handler(conv_handler)

    show_data_handler = CommandHandler("show_data", show_data)
    application.add_handler(show_data_handler)

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# pollbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Basic example for a bot that works with polls. Only 3 people are allowed to interact with each
poll/quiz the bot generates. The preview command generates a closed poll/quiz, exactly like the
one the user sends the bot
"""
import logging

from telegram import (
    KeyboardButton,
    KeyboardButtonPollType,
    Poll,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    PollHandler,
    filters,
)

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

TOTAL_VOTER_COUNT = 3

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inform user about what this bot can do"""
    await update.message.reply_text(
        "Please select /poll to get a Poll, /quiz to get a Quiz or /preview"
        " to generate a preview for your poll"
    )

async def poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a predefined poll"""
    questions = ["Good", "Really good", "Fantastic", "Great"]
    message = await context.bot.send_poll(
        update.effective_chat.id,
        "How are you?",
        questions,
        is_anonymous=False,
        allows_multiple_answers=True,
    )
    # Save some info about the poll the bot_data for later use in receive_poll_answer
    payload = {
        message.poll.id: {
            "questions": questions,
            "message_id": message.message_id,
            "chat_id": update.effective_chat.id,
            "answers": 0,
        }
    }
    context.bot_data.update(payload)

async def receive_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Summarize a users poll vote"""
    answer = update.poll_answer
    answered_poll = context.bot_data[answer.poll_id]
    try:
        questions = answered_poll["questions"]
        # this means this poll answer update is from an old poll, we can't do our answering then
    except KeyError:
        return
    selected_options = answer.option_ids
    answer_string = ""
    for question_id in selected_options:
        if question_id != selected_options[-1]:
            answer_string += questions[question_id] + " and "
        else:
            answer_string += questions[question_id]
    await context.bot.send_message(
        answered_poll["chat_id"],
        f" {update.effective_user.mention_html()} feels {answer_string} !",
        parse_mode=ParseMode.HTML,
    )
    answered_poll["answers"] += 1
    # Close poll after three participants voted
    if answered_poll["answers"] == TOTAL_VOTER_COUNT:
        await context.bot.stop_poll(answered_poll["chat_id"], answered_poll["message_id"])

async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a predefined poll"""
    questions = ["1", "2", "4", "20"]
    message = await update.effective_message.reply_poll(
        "How many eggs do you need for a cake?", questions, type=Poll.QUIZ, correct_option_id=2
    )
    # Save some info about the poll the bot_data for later use in receive_quiz_answer
    payload = {
        message.poll.id: {"chat_id": update.effective_chat.id, "message_id": message.message_id}
    }
    context.bot_data.update(payload)

async def receive_quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Close quiz after three participants took it"""
    # the bot can receive closed poll updates we don't care about
    if update.poll.is_closed:
        return
    if update.poll.total_voter_count == TOTAL_VOTER_COUNT:
        try:
            quiz_data = context.bot_data[update.poll.id]
            # this means this poll answer update is from an old poll, we can't stop it then
        except KeyError:
            return
        await context.bot.stop_poll(quiz_data["chat_id"], quiz_data["message_id"])

async def preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask user to create a poll and display a preview of it"""
    # using this without a type lets the user chooses what he wants (quiz or poll)
    button = [[KeyboardButton("Press me!", request_poll=KeyboardButtonPollType())]]
    message = "Press the button to let the bot generate a preview for your poll"
    # using one_time_keyboard to hide the keyboard
    await update.effective_message.reply_text(
        message, reply_markup=ReplyKeyboardMarkup(button, one_time_keyboard=True)
    )

async def receive_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """On receiving polls, reply to it by a closed poll copying the received poll"""
    actual_poll = update.effective_message.poll
    # Only need to set the question and options, since all other parameters don't matter for
    # a closed poll
    await update.effective_message.reply_poll(
        question=actual_poll.question,
        options=[o.text for o in actual_poll.options],
        # with is_closed true, the poll/quiz is immediately closed
        is_closed=True,
        reply_markup=ReplyKeyboardRemove(),
    )

async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display a help message"""
    await update.message.reply_text("Use /quiz, /poll or /preview to test this bot.")

def main() -> None:
    """Run bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", poll))
    application.add_handler(CommandHandler("quiz", quiz))
    application.add_handler(CommandHandler("preview", preview))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(MessageHandler(filters.POLL, receive_poll))
    application.add_handler(PollAnswerHandler(receive_poll_answer))
    application.add_handler(PollHandler(receive_quiz_answer))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# rawapibot.py

This example uses only the pure, “bare-metal” API wrapper.

```python
#!/usr/bin/env python

"""Simple Bot to reply to Telegram messages.

This is built on the API wrapper, see echobot.py to see the same example built
on the telegram.ext bot framework.
This program is dedicated to the public domain under the CC0 license.
"""
import asyncio
import contextlib
import logging
from typing import NoReturn

from telegram import Bot, Update
from telegram.error import Forbidden, NetworkError

logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

async def main() -> NoReturn:
    """Run the bot."""
    # Here we use the `async with` syntax to properly initialize and shutdown resources.
    async with Bot("TOKEN") as bot:
        # get the first pending update_id, this is so we can skip over it in case
        # we get a "Forbidden" exception.
        try:
            update_id = (await bot.get_updates())[0].update_id
        except IndexError:
            update_id = None

        logger.info("listening for new messages...")
        while True:
            try:
                update_id = await echo(bot, update_id)
            except NetworkError:
                await asyncio.sleep(1)
            except Forbidden:
                # The user has removed or blocked the bot.
                update_id += 1

async def echo(bot: Bot, update_id: int) -> int:
    """Echo the message the user sent."""
    # Request updates after the last update_id
    updates = await bot.get_updates(offset=update_id, timeout=10, allowed_updates=Update.ALL_TYPES)
    for update in updates:
        next_update_id = update.update_id + 1

        # your bot can receive updates without messages
        # and not all messages contain text
        if update.message and update.message.text:
            # Reply to the message
            logger.info("Found message %s !", update.message.text)
            await update.message.reply_text(update.message.text)
            return next_update_id
    return update_id

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt): # Ignore exception when Ctrl-C is pressed
        asyncio.run(main())

```

---

---

# timerbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Simple Bot to send timed Telegram messages.

This Bot uses the Application class to handle the bot and the JobQueue to send
timed messages.

First, a few handler functions are defined. Then, those functions are passed to
the Application and registered at their respective places.
Then, the bot is started and runs until we press Ctrl-C on the command line.

Usage:
Basic Alarm Bot example, sends a message after a set time.
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.

Note:
To use the JobQueue, you must install PTB via
`pip install "python-telegram-bot[job-queue]"`
"""

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# Define a few command handlers. These usually take the two arguments update and
# context.
# Best practice would be to replace context with an underscore,
# since context is an unused local variable.
# This being an example and not having context present confusing beginners,
# we decided to have it present as context.
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends explanation on how to use the bot."""
    await update.message.reply_text("Hi! Use /set <seconds> to set a timer")

async def alarm(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the alarm message."""
    job = context.job
    await context.bot.send_message(job.chat_id, text=f"Beep! {job.data} seconds are over!")

def remove_job_if_exists(name: str, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Remove job with given name. Returns whether job was removed."""
    current_jobs = context.job_queue.get_jobs_by_name(name)
    if not current_jobs:
        return False
    for job in current_jobs:
        job.schedule_removal()
    return True

async def set_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a job to the queue."""
    chat_id = update.effective_message.chat_id
    try:
        # args[0] should contain the time for the timer in seconds
        due = float(context.args[0])
        if due < 0:
            await update.effective_message.reply_text("Sorry we can not go back to future!")
            return

        job_removed = remove_job_if_exists(str(chat_id), context)
        context.job_queue.run_once(alarm, due, chat_id=chat_id, name=str(chat_id), data=due)

        text = "Timer successfully set!"
        if job_removed:
            text += " Old one was removed."
        await update.effective_message.reply_text(text)

    except (IndexError, ValueError):
        await update.effective_message.reply_text("Usage: /set <seconds>")

async def unset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove the job if the user changed their mind."""
    chat_id = update.message.chat_id
    job_removed = remove_job_if_exists(str(chat_id), context)
    text = "Timer successfully cancelled!" if job_removed else "You have no active timer."
    await update.message.reply_text(text)

def main() -> None:
    """Run bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CommandHandler("set", set_timer))
    application.add_handler(CommandHandler("unset", unset))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

---

# webappbot.py

```python
#!/usr/bin/env python

# pylint: disable=unused-argument
# This program is dedicated to the public domain under the CC0 license.

"""
Simple example of a Telegram WebApp which displays a color picker.
The static website for this website is hosted by the PTB team for your convenience.
Currently only showcases starting the WebApp via a KeyboardButton, as all other methods would
require a bot token.
"""
import json
import logging

from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# Enable logging
logging.basicConfig(
    format=" %(asctime)s - %(name)s - %(levelname)s - %(message)s ", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Define a `/start` command handler.
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message with a button that opens a the web app."""
    await update.message.reply_text(
        "Please press the button below to choose a color via the WebApp.",
        reply_markup=ReplyKeyboardMarkup.from_button(
            KeyboardButton(
                text="Open the color picker!",
                web_app=WebAppInfo(url="https://python-telegram-bot.org/static/webappbot"),
            )
        ),
    )

# Handle incoming WebAppData
async def web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Print the received data and remove the button."""
    # Here we use `json.loads`, since the WebApp sends the data JSON serialized string
    # (see webappbot.html)
    data = json.loads(update.effective_message.web_app_data.data)
    await update.message.reply_html(
        text=(
            f"You selected the color with the HEX value <code> {data['hex']} </code>. The "
            f"corresponding RGB value is <code> {tuple(data['rgb'].values())} </code>."
        ),
        reply_markup=ReplyKeyboardRemove(),
    )

def main() -> None:
    """Start the bot."""
    # Create the Application and pass it your bot's token.
    application = Application.builder().token("TOKEN").build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data))

    # Run the bot until the user presses Ctrl-C
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

```

---

## HTML Page

```html
<!--

Simple static Telegram WebApp. Does not verify the WebAppInitData, as a bot token would be needed for that.
-->
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>python-telegram-bot Example WebApp</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/@jaames/iro@5"></script>
</head>
<script type="text/javascript">
    const colorPicker = new iro.ColorPicker('#picker', {
        borderColor: "#ffffff",
        borderWidth: 1,
        width: Math.round(document.documentElement.clientWidth / 2),
    });
    colorPicker.on('color:change', function (color) {
        document.body.style.background = color.hexString;
    });

    Telegram.WebApp.ready();
    Telegram.WebApp.MainButton.setText('Choose Color').show().onClick(function () {
        const data = JSON.stringify({ hex: colorPicker.color.hexString, rgb: colorPicker.color.rgb });
        Telegram.WebApp.sendData(data);
        Telegram.WebApp.close();
    });
</script>
<body style="background-color: #ffffff">
    <div style="position: absolute; margin-top: 5vh; margin-left: 5vw; height: 90vh; width: 90vw; border-radius: 5vh; background-color: var(--tg-theme-bg-color); box-shadow: 0 0 2vw #000000;">
        <div id="picker"
             style="display: flex; justify-content: center; align-items: center; height: 100%; width: 100%"></div>
    </div>
</body>
<script type="text/javascript">
    Telegram.WebApp.expand();
</script>
</html>

```

---

---

# Bot

class telegram. Bot ( token, base_url = 'https://api.telegram.org/bot', base_file_url = 'https://api.telegram.org/file/bot', request = None, get_updates_request = None, private_key = None, private_key_password = None, local_mode = False )
Bases: telegram.TelegramObject, contextlib.AbstractAsyncContextManager

This object represents a Telegram Bot.

Instances of this class can be used as asyncio context managers, where

async with bot: # code is roughly equivalent to

try: await bot. initialize () # code finally: await bot. shutdown ()

See also

__aenter__() and __aexit__().

Note

- Most bot methods have the argument `api_kwargs` which allows passing arbitrary keywords
to the Telegram API. This can be used to access new features of the API before they are
incorporated into PTB. The limitations to this argument are the same as the ones
described in do_api_request().

- Bots should not be serialized since if you for e.g. change the bots token, then your
serialized instance will not reflect that change. Trying to pickle a bot instance will
raise pickle.PicklingError. Trying to deepcopy a bot instance will raise TypeError.

Examples

Raw API Bot

See also

Your First Bot, Builder Pattern

Use In

telegram.ext.ApplicationBuilder.bot()

Available In

- telegram.ext.Application.bot
- telegram.ext.CallbackContext.bot
- telegram.ext.Updater.bot

Added in version 13.2: Objects of this class are comparable in terms of equality. Two objects of this class are
considered equal, if their bot is equal.

Changed in version 20.0:

- Removed the deprecated methods `kick_chat_member`, `kickChatMember`, `get_chat_members_count` and `getChatMembersCount`.
- Removed the deprecated property `commands`.
- Removed the deprecated `defaults` parameter. If you want to use telegram.ext.Defaults, please use the subclass telegram.ext.ExtBot instead.
- Attempting to pickle a bot instance will now raise pickle.PicklingError.
- Attempting to deepcopy a bot instance will now raise TypeError.
- The following are now keyword-only arguments in Bot methods: `location`, `filename`, `venue`, `contact`, `{read, write, connect, pool}_timeout`, `api_kwargs`. Use a named argument for those,
and notice that some positional arguments changed position as a result.

- For uploading files, file paths are now always accepted. If local_mode is False, the file contents will be read in binary mode and uploaded. Otherwise,
the file path will be passed in the file URI scheme.

Changed in version 20.5: Removed deprecated methods `set_sticker_set_thumb` and `setStickerSetThumb`.
Use set_sticker_set_thumbnail() and setStickerSetThumbnail() instead.

Parameters:

- token (str) - Bot’s unique authentication token.
- base_url (str | Callable[[str], str], optional) -
  Telegram Bot API
service URL. If the string contains `{token}`, it will be replaced with the bot’s
token. If a callable is passed, it will be called with the bot’s token as the only
argument and must return the base URL. Otherwise, the token will be appended to the
string. Defaults to `"https://api.telegram.org/bot"`.
  Tip
  Customizing the base URL can be used to run a bot against Local Bot API Server or using Telegrams test environment.
  Example: `"https://api.telegram.org/bot{token}/test"`
  Changed in version 21.11: Supports callable input and string formatting.

- base_file_url (str, optional) -
  Telegram Bot API file URL.
If the string contains `{token}`, it will be replaced with the bot’s
token. If a callable is passed, it will be called with the bot’s token as the only
argument and must return the base URL. Otherwise, the token will be appended to the
string. Defaults to `"https://api.telegram.org/bot"`.
  Tip
  Customizing the base URL can be used to run a bot against Local Bot API Server or using Telegrams test environment.
  Example: `"https://api.telegram.org/file/bot{token}/test"`
  Changed in version 21.11: Supports callable input and string formatting.

- request (telegram.request.BaseRequest, optional) - Pre initialized telegram.request.BaseRequest instances. Will be used for all bot methods *except*for get_updates(). If not passed, an instance of telegram.request.HTTPXRequest will be used.
- get_updates_request (telegram.request.BaseRequest, optional) - Pre initialized telegram.request.BaseRequest instances. Will be used exclusively for get_updates(). If not passed, an instance of telegram.request.HTTPXRequest will be used.
- private_key (bytes, optional) - Private key for decryption of telegram passport data.
- private_key_password (bytes, optional) - Password for above private key.
- local_mode (bool, optional) -
  Set to True, if the base_url is
the URI of a Local Bot API Server that runs with the `--local` flag. Currently, the only effect of
this is that files are uploaded using their local path in the file URI scheme.
Defaults to False.
  Added in version 20.0..

Since this class has a large number of methods and attributes, below you can find a quick overview.

Sending Messages

- send_animation() - Used for sending animations
- send_audio() - Used for sending audio files
- send_chat_action() - Used for sending chat actions
- send_contact() - Used for sending contacts
- send_dice() - Used for sending dice messages
- send_document() - Used for sending documents
- send_game() - Used for sending a game
- send_gift() - Used for sending a gift
- send_invoice() - Used for sending an invoice
- send_location() - Used for sending location
- send_media_group() - Used for sending media grouped together
- send_message() - Used for sending text messages
- send_paid_media() - Used for sending paid media to channels
- send_photo() - Used for sending photos
- send_poll() - Used for sending polls
- send_sticker() - Used for sending stickers
- send_venue() - Used for sending venue locations.
- send_video() - Used for sending videos
- send_video_note() - Used for sending video notes
- send_voice() - Used for sending voice messages
- copy_message() - Used for copying the contents of an arbitrary message
- copy_messages() - Used for copying the contents of an multiple arbitrary messages
- forward_message() - Used for forwarding messages
- forward_messages() - Used for forwarding multiple messages at once

Updating Messages

- answer_callback_query() - Used for answering the callback query
- answer_inline_query() - Used for answering the inline query
- answer_pre_checkout_query() - Used for answering a pre checkout query
- answer_shipping_query() - Used for answering a shipping query
- answer_web_app_query() - Used for answering a web app query
- delete_message() - Used for deleting messages.
- delete_messages() - Used for deleting multiple messages as once.
- edit_message_caption() - Used for editing captions
- edit_message_media() - Used for editing the media on messages
- edit_message_live_location() - Used for editing the location in live location messages
- edit_message_reply_markup() - Used for editing the reply markup on messages
- edit_message_text() - Used for editing text messages
- stop_poll() - Used for stopping the running poll
- set_message_reaction() - Used for setting reactions on messages

Chat Moderation and information

- approve_chat_join_request() - Used for approving a chat join request
- decline_chat_join_request() - Used for declining a chat join request
- ban_chat_member() - Used for banning a member from the chat
- unban_chat_member() - Used for unbanning a member from the chat
- ban_chat_sender_chat() - Used for banning a channel in a channel or supergroup
- unban_chat_sender_chat() - Used for unbanning a channel in a channel or supergroup
- restrict_chat_member() - Used for restricting a chat member
- promote_chat_member() - Used for promoting a chat member
- set_chat_administrator_custom_title() - Used for assigning a custom admin title to an admin
- set_chat_permissions() - Used for setting the permissions of a chat
- export_chat_invite_link() - Used for creating a new primary invite link for a chat
- create_chat_invite_link() - Used for creating an additional invite link for a chat
- edit_chat_invite_link() - Used for editing a non-primary invite link
- revoke_chat_invite_link() - Used for revoking an invite link created by the bot
- set_chat_photo() - Used for setting a photo to a chat
- delete_chat_photo() - Used for deleting a chat photo
- set_chat_title() - Used for setting a chat title
- set_chat_description() - Used for setting the description of a chat
- set_user_emoji_status() - Used for setting the users status emoji
- pin_chat_message() - Used for pinning a message
- unpin_chat_message() - Used for unpinning a message
- unpin_all_chat_messages() - Used for unpinning all pinned chat messages
- get_business_connection() - Used for getting information about the business account.
- get_user_profile_photos() - Used for obtaining user’s profile pictures
- get_chat() - Used for getting information about a chat
- get_chat_administrators() - Used for getting the list of admins in a chat
- get_chat_member_count() - Used for getting the number of members in a chat
- get_chat_member() - Used for getting a member of a chat
- get_user_chat_boosts() - Used for getting the list of boosts added to a chat
- leave_chat() - Used for leaving a chat

Verification on behalf of an organization

- verify_chat() - Used for verifying a chat
- verify_user() - Used for verifying a user
- remove_chat_verification() - Used for removing the verification from a chat
- remove_user_verification() - Used for removing the verification from a user

Bot settings

- set_my_commands() - Used for setting the list of commands
- delete_my_commands() - Used for deleting the list of commands
- get_my_commands() - Used for obtaining the list of commands
- get_my_default_administrator_rights() - Used for obtaining the default administrator rights for the bot
- set_my_default_administrator_rights() - Used for setting the default administrator rights for the bot
- get_chat_menu_button() - Used for obtaining the menu button of a private chat or the default menu button
- set_chat_menu_button() - Used for setting the menu button of a private chat or the default menu button
- set_my_description() - Used for setting the description of the bot
- get_my_description() - Used for obtaining the description of the bot
- set_my_short_description() - Used for setting the short description of the bot
- get_my_short_description() - Used for obtaining the short description of the bot
- set_my_name() - Used for setting the name of the bot
- get_my_name() - Used for obtaining the name of the bot

Stickerset management

- add_sticker_to_set() - Used for adding a sticker to a set
- delete_sticker_from_set() - Used for deleting a sticker from a set
- create_new_sticker_set() - Used for creating a new sticker set
- delete_sticker_set() - Used for deleting a sticker set made by a bot
- set_chat_sticker_set() - Used for setting a sticker set of a chat
- delete_chat_sticker_set() - Used for deleting the set sticker set of a chat
- replace_sticker_in_set() - Used for replacing a sticker in a set
- set_sticker_position_in_set() - Used for moving a sticker’s position in the set
- set_sticker_set_title() - Used for setting the title of a sticker set
- set_sticker_emoji_list() - Used for setting the emoji list of a sticker
- set_sticker_keywords() - Used for setting the keywords of a sticker
- set_sticker_mask_position() - Used for setting the mask position of a mask sticker
- set_sticker_set_thumbnail() - Used for setting the thumbnail of a sticker set
- set_custom_emoji_sticker_set_thumbnail() - Used for setting the thumbnail of a custom emoji sticker set
- get_sticker_set() - Used for getting a sticker set
- upload_sticker_file() - Used for uploading a sticker file
- get_custom_emoji_stickers() - Used for getting custom emoji files based on their IDs

Games

- get_game_high_scores() - Used for getting the game high scores
- set_game_score() - Used for setting the game score

Getting updates

- get_updates() - Used for getting updates using long polling
- get_webhook_info() - Used for getting current webhook status
- set_webhook() - Used for setting a webhook to receive updates
- delete_webhook() - Used for removing webhook integration

Forum topic management

- close_forum_topic() - Used for closing a forum topic
- close_general_forum_topic() - Used for closing the general forum topic
- create_forum_topic() - Used to create a topic
- delete_forum_topic() - Used for deleting a forum topic
- edit_forum_topic() - Used to edit a topic
- edit_general_forum_topic() - Used to edit the general topic
- get_forum_topic_icon_stickers() - Used to get custom emojis to use as topic icons
- hide_general_forum_topic() - Used to hide the general topic
- unhide_general_forum_topic() - Used to unhide the general topic
- reopen_forum_topic() - Used to reopen a topic
- reopen_general_forum_topic() - Used to reopen the general topic
- unpin_all_forum_topic_messages() - Used to unpin all messages in a forum topic
- unpin_all_general_forum_topic_messages() - Used to unpin all messages in the general forum topic

Payments and Stars

- create_invoice_link() - Used to generate an HTTP link for an invoice
- edit_user_star_subscription() - Used for editing a user’s star subscription
- get_star_transactions() - Used for obtaining the bot’s Telegram Stars transactions
- refund_star_payment() - Used for refunding a payment in Telegram Stars

Miscellaneous

- close() - Used for closing server instance when switching to another local server
- log_out() - Used for logging out from cloud Bot API server
- get_file() - Used for getting basic info about a file
- get_available_gifts() - Used for getting information about gifts available for sending
- get_me() - Used for getting basic information about the bot
- save_prepared_inline_message() - Used for storing a message to be sent by a user of a Mini App

Properties

- base_file_url - Telegram Bot API file URL
- base_url - Telegram Bot API service URL
- bot - The user instance of the bot as returned by get_me()
- can_join_groups - Whether the bot can join groups
- can_read_all_group_messages - Whether the bot can read all incoming group messages
- id - The user id of the bot
- name - The username of the bot, with leading `@`
- first_name - The first name of the bot
- last_name - The last name of the bot
- local_mode - Whether the bot is running in local mode
- username - The username of the bot, without leading `@`
- link - The t.me link of the bot
- private_key - Deserialized private key for decryption of telegram passport data
- supports_inline_queries - Whether the bot supports inline queries
- token - Bot’s unique authentication token

async __aenter__ ( )
Asynchronous context manager which initializes the Bot.

Returns: The initialized Bot instance.

Raises: Exception – If an exception is raised during initialization, shutdown() is called in this case.

async __aexit__ ( exc_type, exc_val, exc_tb )
Asynchronous context manager which shuts down the Bot.

__deepcopy__ ( memodict )
Customizes how copy.deepcopy() processes objects of this type. Bots can not
be deepcopied and this method will always raise an exception.

Added in version 20.0.

Raises: TypeError –

__eq__ ( other )
Defines equality condition for the telegram.Bot object.
Two objects of this class are considered to be equal if their attributes bot are equal.

Returns: True if both attributes bot are equal. False otherwise.

__hash__ ( )
See telegram.TelegramObject.__hash__()

__reduce__ ( )
Customizes how copy.deepcopy() processes objects of this type. Bots can not
be pickled and this method will always raise an exception.

Added in version 20.0.

Raises: pickle.PicklingError –

__repr__ ( )
Give a string representation of the bot in the form `Bot[token=...]`.

As this class doesn’t implement object.__str__(), the default implementation
will be used, which is equivalent to __repr__().

Returns: str

async addStickerToSet ( user_id, name, sticker, *, read_timeout = None, write_timeout = None, connect_timeout = None, pool_timeout = None, api_kwargs = None )
Alias for add_sticker_to_set()

async add_sticker_to_set ( user_id, name, sticker, *, read_timeout = None, write_timeout = None, connect_timeout = None, pool_timeout = None, api_kwargs = None )
Use this method to add a new sticker to a set created by the bot. The format of the added
sticker must match the format of the other stickers in the set. Emoji sticker sets can have
up to 200 stickers. Other
sticker sets can have up to 120 stickers.

Changed in version 20.2: Since Bot API 6.6, the parameter sticker replace the parameters `png_sticker`, `tgs_sticker`, `webm_sticker`, `emojis`, and `mask_position`.

Changed in version 20.5: Removed deprecated parameters `png_sticker`, `tgs_sticker`, `webm_sticker`, `emojis`, and `mask_position`.

Parameters:

- user_id (int) - User identifier of created sticker set owner.
- name (str) - Sticker set name.
- sticker (telegram.InputSticker) -
  An object with information about the added
sticker. If exactly the same sticker had already been added to the set, then the
set isn’t changed.
  Added in version 20.2.

Keyword Arguments:

- read_timeout (float | None, optional) - Value to pass to telegram.request.BaseRequest.post.read_timeout. Defaults to DEFAULT_NONE.
- write_timeout (float | None, optional) -
  Value to pass to telegram.request.BaseRequest.post.write_timeout. By default, `20` seconds are used as write timeout.
  Changed in version 22.0: The default value changed to DEFAULT_NONE.

- connect_timeout (float | None, optional) - Value to pass to telegram.request.BaseRequest.post.connect_timeout. Defaults to DEFAULT_NONE.
- pool_timeout (float | None, optional) - Value to pass to telegram.request.BaseRequest.post.pool_timeout. Defaults to DEFAULT_NONE.
- api_kwargs (dict, optional) - Arbitrary keyword arguments to be passed to the Telegram API. See do_api_request() for limitations.

Returns: On success, True is returned.

Return type: bool

Raises: telegram.error.TelegramError –

async answerCallbackQuery ( callback_query_id, text = None, show_alert = None, url = None, cache_time = None, *, read_timeout = None, write_timeout = None, connect_timeout = None, pool_timeout = None, api_kwargs = None )
Alias for answer_callback_query()

async answerInlineQuery ( inline_query_id, results, cache_time = None, is_personal = None, next_offset = None, button = None, *, current_offset = None, read_timeout = None, write_timeout = None, connect_timeout = None, pool_timeout = None, api_kwargs = None )
Alias for answer_inline_query()

async answerPreCheckoutQuery ( pre_checkout_query_id, ok, error_message = None, *, read_timeout = None, write_timeout = None, connect_timeout = None, pool_timeout = None, api_kwargs = None )
Alias for answer_pre_checkout_query()

async answerShippingQuery ( shipping_query_id, ok, shipping_options = None, error_message = None, *, read_timeout = None, write_timeout = None, connect_timeout = None, pool_timeout = None, api_kwargs = None )
Alias for answer_shipping_query()

async answerWebAppQuery ( web_app_query_id, result, *, read_timeout = None, write_timeout = None, connect_timeout = None, pool_timeout = None, api_kwargs = None )
Alias for answer_web_app_query()

async answer_callback_query ( callback_query_id, text = None, show_alert = None, url = None, cache_time = None, *, read_timeout = None, write_timeout = None, connect_timeout = None, pool_timeout = None, api_kwargs = None )
Use this method to send answers to callback queries sent from inline keyboards. The answer
will be displayed to the user as a notification at the top of the chat screen or as an
alert.
Alternatively, the user can be redirected to the specified Game URL. For this option to
work, you must first create a game for your bot via @BotFather and accept the terms. Otherwise, you may use links like t.me/your_bot?start=XXXX that open
your bot with a parameter.

Shortcuts

telegram.CallbackQuery.answer()

Parameters:

- callback_query_id (str) - Unique identifier for the query to be answered.
- text (str, optional) - Text of the notification. If not specified, nothing will
be shown to the user, 0-200 characters.

- show_alert (bool, optional) - If True, an alert will be shown by the
client instead of a notification at the top of the chat screen. Defaults to False.

- url (str, optional) -
  URL that will be opened by the user’s client. If you have
created a Game and accepted the conditions via @BotFather, specify the URL that
opens your game - note that this will only work if the query comes from a callback
game button. Otherwise, you may use links like t.me/your_bot?start=XXXX that open
your bot with a parameter.

- cache_time (int | datetime.timedelta, optional) -
  The maximum amount of
time in seconds that the
result of the callback query may be cached client-side. Defaults to 0.
  Changed in version 21.11: datetime.timedelta objects are accepted in addition to plain int values.

Keyword Arguments:

- read_timeout (float | None, optional) - Value to pass to telegram.request.BaseRequest.post.read_timeout. Defaults to DEFAULT_NONE.
- write_timeout (float | None, optional) - Value to pass to telegram.request.BaseRequest.post.write_timeout. Defaults to DEFAULT_NONE.
- connect_timeout (float | None, optional) - Value to pass to telegram.request.BaseRequest.post.connect_timeout. Defaults to DEFAULT_NONE.
- pool_timeout (float | None, optional) - Value to pass to telegram.request.BaseRequest.post.pool_timeout. Defaults to DEFAULT_NONE.
- api_kwargs (dict, optional) - Arbitrary keyword arguments to be passed to the Telegram API. See do_api_request() for limitations.

Returns: bool On success, True is returned.

Return type: bool

Raises: telegram.error.TelegramError –

async answer_inline_query ( inline_query_id, results, cache_time = None, is_personal = None, next_offset = None, button = None, *, current_offset = None, read_timeout = None, write_timeout = None, connect_timeout = None, pool_timeout = None, api_kwargs = None )
Use this method to send answers to an inline query. No more than 50 results per query are allowed.

Warning

In most use cases current_offset should not be passed manually. Instead of
calling this method directly, use the shortcut telegram.InlineQuery.answer() with telegram.InlineQuery.answer.auto_pagination set to True, which will
take care of passing the correct value.

See also

Working with Files and Media

Shortcuts

telegram.InlineQuery.answer()

Changed in version 20.5: Removed deprecated arguments `switch_pm_text` and `switch_pm_parameter`.

Parameters:

- inline_query_id (str) - Unique identifier for the answered query.
- results (list[telegram.InlineQueryResult] | Callable) - A list of results for
the inline query. In case current_offset is passed, results may also be
a callable that accepts the current page index starting from 0. It must return
either a list of telegram.InlineQueryResult instances or None if
there are no more results.

- cache_time (int | datetime.timedelta, optional) -
  The maximum amount of
time in seconds that the
result of the inline query may be cached on the server. Defaults to `300`.
  Changed in version 21.11: datetime.timedelta objects are accepted in addition to plain int values.

- is_personal (bool, optional) - Pass True, if results may be cached on
the server side only for the user that sent the query. By default,
results may be returned to any user who sends the same query.

- next_offset (str, optional) - Pass the offset that a client should send in the
next query with the same text to receive more results. Pass an empty string if
there are no more results or if you don’t support pagination. Offset length can’t
exceed 64 bytes.

- button (telegram.inlinequeryresultsbutton.InlineQueryResultsButton, optional) -
  A button to be shown
above the inline query results.
  Added in version 20.3.

Keyword Arguments:

- current_offset (str, optional) - The telegram.InlineQuery.offset of
the inline query to answer. If passed, PTB will automatically take care of
the pagination for you, i.e. pass the correct next_offset and truncate
the results list/get the results from the callable you passed.

- read_timeout (float | None, optional) - Value to pass to telegram.request.BaseRequest.post.read_timeout. Defaults to DEFAULT_NONE.
- write_timeout (float | None, optional) - Value to pass to telegram.request.BaseRequest.post.write_timeout. Defaults to DEFAULT_NONE.
- connect_timeout (float | None, optional) - Value to pass to telegram.request.BaseRequest.post.connect_timeout. Defaults to DEFAULT_NONE.
- pool_timeout (float | None, optional) - Value to pass to telegram.request.BaseRequest.post.pool_timeout. Defaults to DEFAULT_NONE.
- api_kwargs (dict, optional) - Arbitrary keyword arguments to be passed to the Telegram API. See do_api_request() for limitations.

Returns: On success, True is returned.

Return type: bool

Raises: telegram.error.TelegramError –

async answer_pre_checkout_query ( pre_checkout_query_id, ok, error_message = None, *, read_timeout = None, write_timeout = None, connect_timeout = None, pool_timeout = None, api_kwargs = None )
Once the user has confirmed their payment and shipping details, the Bot API sends the final
confirmation in the form of an telegram.Update with the field telegram.Update.pre_checkout_query. Use this method to respond to such pre-checkout
queries.

Note

The Bot API must receive an answer within 10 seconds after the pre-checkout
query was sent.

Shortcuts

telegram.precheckoutquery.PreCheckoutQuery.answer()

Parameters:

- pre_checkout_query_id (str) - Unique identifier for the query to be answered.
- ok (bool) - Specify True if everything is alright
(goods are available, etc.) and the bot is ready to proceed with the order. Use False if there are any problems.

- error_message (str, optional) - Required if ok is False. Error
message in human readable form
