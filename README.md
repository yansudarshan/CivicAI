# Civic AI Hackathon MVP

Responsive citizen + authority civic grievance portal.

- **Frontend:** responsive HTML/CSS/JavaScript
- **Backend:** FastAPI + Uvicorn
- **Database:** MongoDB Atlas
- **AI:** OpenAI
- **Workflow:** LangGraph
- **Authentication:** JWT + Argon2 password hashing

## Requirements

- Python 3.11+
- MongoDB Atlas account/cluster
- OpenAI API key

## 1. Create virtual environment

### Windows PowerShell

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, use:

```powershell
.venv\Scripts\activate.bat
```

### macOS/Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

## 3. Configure environment

Copy `.env.example` to `.env`:

### Windows

```powershell
copy .env.example .env
```

### macOS/Linux

```bash
cp .env.example .env
```

Edit `.env` and add your MongoDB Atlas connection string and OpenAI API key.

```env
MONGODB_URI=mongodb+srv://USERNAME:PASSWORD@CLUSTER.mongodb.net/?retryWrites=true&w=majority
MONGODB_DATABASE=civic_ai
JWT_SECRET=replace-with-a-long-random-secret
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-5.6
OPENAI_TRANSCRIBE_MODEL=gpt-4o-mini-transcribe
AUTHORITY_EMAIL=authority@civic.ai
AUTHORITY_PASSWORD=Demo@12345
```

> If your OpenAI account exposes a different current model ID, set `OPENAI_MODEL` to that model ID.

## 4. Run the application

```bash
uvicorn app.main:app --reload
```

Then open:

**http://localhost:8000**

API documentation:

**http://localhost:8000/docs**

Health check:

**http://localhost:8000/health**

## Demo authority account

```text
Email:    authority@civic.ai
Password: Demo@12345
```

The authority account is automatically created in MongoDB on application startup if it does not already exist.

## Main demo flow

1. Citizen signs up/logs in.
2. Citizen reports a civic issue using text, voice, image and/or location.
3. LangGraph runs AI analysis, routing and duplicate detection.
4. Complaint is saved to MongoDB with a ticket ID.
5. Citizen sees the complaint in history and tracks its status.
6. Authority logs in and sees assigned complaints.
7. Authority starts work and later submits a completion message/evidence.
8. AI verifies the completion.
9. Citizen sees the authority response, verification result and civic credits.

## Project structure

```text
civic-ai-hackathon/
├── README.md
├── .env.example
├── requirements.txt
├── media/
└── app/
    ├── main.py
    └── static/
        ├── index.html
        ├── style.css
        └── app.js
```

No Docker is required. MongoDB Atlas is accessed directly from the Python application through PyMongo's async client.
