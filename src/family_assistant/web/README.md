# Web Layer

This package is the FastAPI application layer for the Family Assistant: the REST API, the
authentication middleware, and the serving of the React frontend. The user interface itself is a
React app in [`frontend/`](../../../frontend); this layer serves its built assets and provides the
API it talks to.

## Layout

Core modules:

- `app_creator.py` — the application factory (`create_app()`), plus the module-level `app` singleton
  used by `uvicorn family_assistant.web.app_creator:app`. Configures middleware, mounts `/static`,
  and registers the routers.
- `auth.py` — OIDC session authentication, API-token authentication, and `AuthMiddleware` with its
  list of public paths.
- `dependencies.py` — FastAPI dependency providers for the database context, services, and the
  current user.
- `models.py` — Pydantic request and response models.
- `utils.py`, `template_utils.py`, `audio_utils.py` — shared helpers.
- `web_chat_interface.py`, `turn_producer.py`, `conversation_stream_hub.py`,
  `web_mid_turn_controller.py`, `confirmation_manager.py`, `web_confirmation_ui_manager.py` — the
  chat transport: streaming turns to connected clients and driving tool-confirmation prompts.
- `voice_client.py`, `resources/` — voice mode support and its greeting audio.

`routers/` holds one router per feature area. `api.py` is the aggregator that mounts the `*_api.py`
routers under `/api`, and `vite_pages.py` serves the React entry points and service worker. The
remaining routers — webhooks, health, client config, push and iOS push, A2A discovery, UCP, app
auth, the context viewer, and the live voice APIs — are registered directly in `app_creator.py`,
which is the authoritative list.

## Templates and static files

The UI is React, but a few server-rendered pages remain. Jinja2 templates live in
[`../templates/`](../templates) (`base.html.j2`, `context_viewer.html.j2`) and are used by the
context viewer. Static assets are served from [`../static/`](../static); the production frontend
build lands in `../static/dist/`.

`routers/vector_search.py` and `routers/errors.py` still render Jinja2 templates, but they are not
registered with the app and the templates they name no longer exist — treat them as vestigial.

## Development

See [CLAUDE.md](CLAUDE.md) in this directory for the development guide: adding endpoints, dependency
injection, the auth flow, and testing.
