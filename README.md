# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for managing employee records, tracking attendance, and providing employee and department-level analytics.

## Features

- Create and list employee records.
- Record employee punch-in and punch-out times.
- Track attendance status and working hours.
- Calculate late arrivals, overtime, and half-day attendance.
- Retrieve attendance records with filters and pagination.
- Regularize attendance records.
- Generate monthly employee attendance reports.
- Generate department attendance summaries.
- Rank employees by late-arrival minutes.
- View department attendance trends.
- Inspect database query execution plans.

## Technology Stack

- Python 3.11+
- FastAPI
- MongoDB 6.0+
- PyMongo
- Uvicorn
- Pydantic
- python-dotenv

## Prerequisites

Install Python 3.11 or newer and MongoDB 6.0 or newer.

## Setup

### 1. Open the project directory

```bash
cd attendance_api_submission
```

### 2. Create and activate a virtual environment

On Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure MongoDB

Set the following environment variables:

- `MONGO_URI` — MongoDB connection string.
- `MONGO_DB` — Database name.

For a local MongoDB instance, the values can be:

```text
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

The application uses `python-dotenv` to load a local `.env` file when configured. Never commit credentials or the `.env` file to a public repository.

### 5. Run the API

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## API Documentation

Once the server is running, open:

- **Swagger UI:** http://127.0.0.1:8000/docs
- **ReDoc:** http://127.0.0.1:8000/redoc
- **Health check:** http://127.0.0.1:8000/health

Swagger UI can be used to explore endpoints and send test requests.

## Database

MongoDB stores employee details and attendance records. Configure the connection URI and database name before starting the application.

## Testing

Use Swagger UI or Postman to test employee creation, attendance punch-in and punch-out, filtering, pagination, regularization, analytics, and query execution plans.

Verify duplicate requests, timestamp handling, attendance calculations, and pagination behavior.

## Project Structure

```text
attendance_api_submission/
├── app/
│   └── main.py
├── requirements.txt
├── README.md
├── DECISIONS.md
└── REVIEW.md
```

Other files, such as sample data and configuration templates, may also be included.

## Design Decisions

See [DECISIONS.md](DECISIONS.md) for the reasoning behind database indexes, concurrent punch-in handling, leaderboard ties, department headcount, and scaling considerations.

## Review

See [REVIEW.md](REVIEW.md) for the implementation review.

## License

Developed as part of a software engineering assignment. No license has been specified.
