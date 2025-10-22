# Silae Shift Sync

Automatically sync your Silae work shifts to Google Calendar using Docker.

## Features

- 🔐 **Secure**: All credentials stored as environment variables
- 🐳 **Containerized**: Runs in Docker for consistent deployment
- 📅 **Smart Sync**: Automatically replaces existing events to avoid duplicates
- 🔄 **Weekly Schedule**: Syncs next week's shifts every Sunday
- 📊 **Logging**: Comprehensive logging for monitoring and debugging
- ⚙️ **Configurable**: Timezone and other settings via environment variables

## Prerequisites

- Docker and Docker Compose
- Google Cloud Project with Calendar API enabled
- Silae account credentials
- Google Calendar with calendar ID

## Setup

### 1. Environment Setup

```bash
# Copy environment template
cp .env.example .env

# Create directories
mkdir -p credentials logs
```

### 2. Add Google Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Enable the Google Calendar API
4. Create OAuth 2.0 credentials
5. Download the JSON file
6. Place it at `credentials/google_credentials.json`
7. 

### 3. Configure Environment Variables

Edit the `.env` file 

```env
# Silae Portal Credentials
SILAE_USERNAME=your_silae_username
SILAE_PASSWORD=your_silae_password
EMPLOYEE_ID=your_employee_id

# Google Calendar Configuration
HOTEL_CALENDAR_ID=your_hotel_calendar_id@group.calendar.google.com

# Optional Settings
TIMEZONE=Europe/Paris
LOG_LEVEL=INFO
```

### 4. Build and Run

```bash
# Build the Docker image
docker-compose build

# Run once
docker-compose run --rm shift-sync

# Or run as service
docker-compose up -d
```

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SILAE_USERNAME` | ✅ | - | Your Silae portal username |
| `SILAE_PASSWORD` | ✅ | - | Your Silae portal password |
| `EMPLOYEE_ID` | ✅ | - | Your employee ID in Silae |
| `HOTEL_CALENDAR_ID` | ✅ | - | Google Calendar ID to sync to |
| `TIMEZONE` | ❌ | Europe/Paris | Timezone for events |
| `LOG_LEVEL` | ❌ | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |

### Getting Your Calendar ID

Run this in your Jupyter notebook to get the calendar ID:

```python
from calendar_api import GoogleCalendarAPI

cal_api = GoogleCalendarAPI('credentials/google_credentials.json', 'credentials/token.pickle')
calendars = cal_api.list_calendars()

# Find your hotel calendar and copy its ID
```

### Getting your employee ID
Run this in your Jupyter notebook to get your employee ID:

```python
from utils import login_silae_portal

session = login_silae_portal(silae_username, silae_password)
resources = get_planning_resources(session)
for resource in resources:
    print(f"Resource Name: {resource['name']}, Employee ID: {resource['id']}")

# Find your employee name and copy its ID
```


## Docker Commands

```bash
# Build image
docker-compose build

# Run once (foreground)
docker-compose run --rm shift-sync

# Run as service (background)
docker-compose up -d

# Stop service
docker-compose down

# View logs
docker-compose logs -f shift-sync

# Restart service
docker-compose restart shift-sync

# View running containers
docker-compose ps
```

## Scheduling

### Option 1: Docker Compose (Recommended)

The docker-compose service will run the sync and then exit. You can schedule it using:

**Windows Task Scheduler:**
1. Create a new task
2. Set trigger for "Weekly" on Sunday
3. Action: Start a program
4. Program: `docker-compose`
5. Arguments: `run --rm shift-sync`
6. Start in: Your project directory

**Linux Cron:**
```bash
# Edit crontab
crontab -e

# Add weekly run (Sundays at 6 AM)
0 6 * * 0 cd /path/to/project && docker-compose run --rm shift-sync
```

### Option 2: Continuous Service

Uncomment the last line in `docker-compose.yml` to run as a continuous service that syncs weekly.

## Troubleshooting

### Authentication Issues

1. **Google Auth Error**: Ensure your `google_credentials.json` is valid and the Calendar API is enabled
2. **Silae Login Failed**: Check your username/password in `.env`
3. **Calendar Not Found**: Verify your `HOTEL_CALENDAR_ID` is correct

### Docker Issues

```bash
# Rebuild completely
docker-compose down
docker-compose build --no-cache
docker-compose up

# Check logs
docker-compose logs shift-sync

# Access container shell
docker-compose run --rm shift-sync bash
```

### Permission Issues

If you encounter permission errors:

```bash
# Fix ownership (Linux/Mac)
sudo chown -R $USER:$USER credentials logs

# Or run with different user in Docker
docker-compose run --rm --user $(id -u):$(id -g) shift-sync
```

## Security Notes

- Never commit `.env` files or credential files to git
- The `.gitignore` file prevents accidental commits
- Google tokens are automatically refreshed
- Use environment variables in production
- Consider using Docker secrets for enhanced security

## File Structure

```
silae_planning/
├── weekly_shift_sync.py    # Main sync script
├── calendar_api.py         # Google Calendar API wrapper
├── config.py              # Configuration management
├── utils.py               # Silae API utilities
├── requirements.txt       # Python dependencies
├── Dockerfile            # Docker image definition
├── docker-compose.yml    # Docker service configuration
├── .env.example          # Environment template
├── .gitignore           # Git ignore rules
└── README.md            # This file
```

## Development

To modify the code:

1. Edit the Python files
2. Rebuild the Docker image: `docker-compose build`
3. Test: `docker-compose run --rm shift-sync`

For local development without Docker:

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export SILAE_USERNAME="your_username"
export SILAE_PASSWORD="your_password"
# ... other variables

# Run directly
python weekly_shift_sync.py
```