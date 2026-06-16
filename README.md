# TBC Ambassador Bot

A Telegram bot for the TBC Bank ambassador program. Users go through an onboarding flow, generate a composite selfie with a TBC ambassador via Gemini 3 Pro Image (Nano Banana Pro), and receive the result after admin moderation.

## Features

- Bilingual (Russian / Uzbek) bot flow
- Privacy policy agreement step
- Telegram channel subscription check
- AI-generated composite selfie using `gemini-3-pro-image`
- TBC logo watermark (Pillow)
- Admin moderation queue with Telegram notifications
- Approve / Reject directly in Telegram
- Web admin panel (FastAPI + Bootstrap 5)
- Budget tracking and limit enforcement
- Basic analytics with CSV export

## Tech Stack

| Layer | Technology |
|---|---|
| Bot framework | [aiogram 3.x](https://docs.aiogram.dev) |
| Web / Admin API | [FastAPI](https://fastapi.tiangolo.com) |
| Admin UI | Jinja2 + Bootstrap 5 |
| Database | PostgreSQL (Railway addon) |
| ORM | SQLAlchemy 2.x async + asyncpg |
| Migrations | Alembic |
| AI | Google Gemini 3 Pro Image (`gemini-3-pro-image`) |
| Image processing | Pillow |
| Deployment | [Railway](https://railway.app) |

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/mikitavalkunovich-innowise/TBC-Ambassador-Bot.git
cd TBC-Ambassador-Bot
```

### 2. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
# Edit .env with your values
```

Required variables:
- `ADMIN_USERNAME` — admin panel username
- `ADMIN_PASSWORD` — admin panel password
- `SECRET_KEY` — session signing key (`python -c "import secrets; print(secrets.token_hex(32))"`)
- `GOOGLE_AI_API_KEY` — Google AI Studio API key
- `DATABASE_URL` — PostgreSQL connection string
- `WEBHOOK_BASE_URL` — your public URL (for Telegram webhook)

### 4. Run database migrations

```bash
alembic upgrade head
```

### 5. Run the app

```bash
uvicorn app.main:app --reload --port 8000
```

### 6. Configure via admin panel

Open `http://localhost:8000/admin` and log in. Go to **Settings** to configure:
1. Bot token (required to activate the bot)
2. Telegram channel ID for subscription check
3. Admin Telegram user ID for moderation notifications
4. Ambassador photo and TBC logo
5. Video URL/file per language
6. All bot message texts

## Railway Deployment

1. Create a new Railway project
2. Add a PostgreSQL addon
3. Add a Volume (mount at `/data`)
4. Set all environment variables from `.env.example`
5. Deploy — the `railway.toml` handles build and start commands

The app runs migrations automatically on startup (`alembic upgrade head`).

## Admin Panel

Available at `/admin/`. Sections:

| Section | Description |
|---|---|
| Dashboard | User stats, images generated, budget usage |
| Settings | All bot configuration (tabbed) |
| Moderation | Image approval queue |
| Analytics | Export CSV |

## Architecture

```
Railway Service
├── FastAPI app (uvicorn)
│   ├── POST /bot/webhook     ← Telegram updates
│   ├── GET  /admin/*         ← Admin panel
│   └── GET  /health          ← Health check
├── PostgreSQL
└── Persistent volume /data
    └── uploads/
        ├── ambassador/
        ├── logo/
        ├── videos/
        └── generated/
```

## Project Structure

```
app/
├── main.py              # FastAPI app + lifespan
├── bot/                 # aiogram bot
│   ├── instance.py      # Bot/Dispatcher singleton
│   ├── states.py        # FSM states
│   ├── router.py        # Dispatcher setup
│   ├── handlers/        # Update handlers
│   ├── keyboards/       # Keyboard builders
│   └── middlewares/     # DB session injection
├── admin/               # Admin panel
│   ├── auth.py          # Session auth
│   ├── router.py        # Router assembly
│   ├── routes/          # Route handlers
│   └── templates/       # Jinja2 HTML templates
├── core/                # Shared infrastructure
│   ├── config.py        # Pydantic Settings
│   ├── database.py      # Async engine + session
│   └── storage.py       # File storage helpers
├── models/              # SQLAlchemy models
└── services/            # Business logic
```
