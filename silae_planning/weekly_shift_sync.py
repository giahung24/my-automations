"""
Weekly Shift Sync Script
Runs every Sunday to sync Silae shifts with Google Calendar for the next 4 weeks.
"""

import sys
import logging
from datetime import datetime, timedelta
import pytz

from config import config
from utils import (
    get_planning_events,
    get_employee_shifts,
    parse_silae_time,
    SilaeSession,
)
from calendar_api import GoogleCalendarAPI


logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s - %(levelname)s - %(message)s",
)


class WeeklyShiftSync:
    def __init__(self):
        """Initialize the sync service using configuration."""
        config.ensure_credentials_dir()

        self.calendar_api = GoogleCalendarAPI(
            credentials_file=config.get_credentials_path(),
            token_file=config.get_token_path(),
        )

        self.hotel_calendar_id = config.HOTEL_CALENDAR_ID
        self.silae_username = config.SILAE_USERNAME
        self.silae_password = config.SILAE_PASSWORD
        self.employee_id = config.EMPLOYEE_ID
        self.base_url = config.SILAE_BASE_URL

        self.timezone = pytz.timezone(config.TIMEZONE)
        self.logger = logging.getLogger(__name__)

    def get_next_4_week_dates(self):
        """Get date range from today to 27 days later (4-week window)."""
        today = datetime.now()
        end_date = today + timedelta(days=27)
        return today.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")

    def get_existing_calendar_events(self, start_date, end_date):
        """Get existing events in the hotel calendar for the date range."""
        return self.calendar_api.get_events_in_range(
            self.hotel_calendar_id, start_date, end_date
        )

    def delete_calendar_event(self, event_id):
        """Delete a calendar event."""
        success = self.calendar_api.delete_event(event_id, self.hotel_calendar_id)
        if success:
            self.logger.info(f"Deleted existing calendar event: {event_id}")
        return success

    def create_calendar_event(self, shift):
        """Create a calendar event from a Silae shift."""
        start_time = parse_silae_time(shift["start"])
        end_time = parse_silae_time(shift["end"])

        if not start_time or not end_time:
            self.logger.error(f"Failed to parse times for shift {shift.get('id')}")
            return None

        description = f"""
Shift Details:
- Code: {shift['code']} ({shift['label']})
- Duration: {shift['durationText']}
- Site: {shift['siteName']}
- Break: {shift['breakTime']} min
- Description: {shift.get('description', '').strip()}
""".strip()

        reminders = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 30},
            ],
        }

        extended_properties = {
            "private": {
                "silae_shift_id": str(shift["id"]),
                "silae_shift_code": shift["code"],
                "sync_script": "weekly_shift_sync",
            }
        }

        created_event = self.calendar_api.create_event(
            summary=shift["label"],
            start_time=start_time,
            end_time=end_time,
            description=description,
            location=shift["siteName"],
            calendar_id=self.hotel_calendar_id,
            timezone=config.TIMEZONE,
            color_id="1",
            reminders=reminders,
            extended_properties=extended_properties,
        )

        if created_event:
            self.logger.info(
                f'Created calendar event: {shift["label"]} on {start_time.strftime("%Y-%m-%d %H:%M")}'
            )

        return created_event

    def get_events_for_date(self, existing_events, target_date):
        """Get events that occur on a specific date."""
        return self.calendar_api.get_events_for_date(existing_events, target_date)

    def sync_shifts_to_calendar(self):
        """Main sync function."""
        self.logger.info("Starting weekly shift sync...")
        self.logger.info(f"Sync time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        try:
            self.logger.info("Logging into Silae...")
            adapter = SilaeSession(
                self.silae_username,
                self.silae_password,
                self.base_url,
                verify_ssl=config.SILAE_VERIFY_SSL,
                ca_bundle=config.SILAE_CA_BUNDLE,
            )
            session = adapter.session

            if session is None:
                self.logger.error("Failed to login to Silae portal")
                return False

            start_date_str, end_date_str = self.get_next_4_week_dates()
            self.logger.info(
                f"Syncing shifts for period: {start_date_str} to {end_date_str}"
            )

            self.logger.info("Fetching planning events from Silae...")
            planning_events = get_planning_events(
                session, date_from=start_date_str, date_to=end_date_str, view="week"
            )

            if not planning_events:
                self.logger.info("No planning events found for this period")
                return True

            shifts = get_employee_shifts(planning_events, self.employee_id)
            work_shifts = [shift for shift in shifts if shift.get("type") == "WORK"]
            self.logger.info(f"Found {len(work_shifts)} work shifts")

            start_datetime = datetime.strptime(start_date_str, "%Y-%m-%d")
            end_datetime = datetime.strptime(end_date_str, "%Y-%m-%d") + timedelta(
                days=1
            )
            start_datetime = self.timezone.localize(start_datetime)
            end_datetime = self.timezone.localize(end_datetime)

            existing_events = self.get_existing_calendar_events(
                start_datetime, end_datetime
            )
            self.logger.info(f"Found {len(existing_events)} existing calendar events")

            for shift in work_shifts:
                shift_date = parse_silae_time(shift["start"])
                if not shift_date:
                    continue

                self.logger.info(
                    f"Processing shift: {shift['label']} on {shift_date.strftime('%Y-%m-%d %H:%M')}"
                )

                events_on_date = self.get_events_for_date(existing_events, shift_date)
                for existing_event in events_on_date:
                    self.logger.info(
                        f"Deleting existing event: {existing_event.get('summary', 'Untitled')}"
                    )
                    self.delete_calendar_event(existing_event["id"])

                self.create_calendar_event(shift)

            self.logger.info(
                f"Sync completed successfully! Processed {len(work_shifts)} work shifts."
            )
            return True
        except Exception as error:
            self.logger.error(f"ERROR during sync: {error}")
            return False

    def run(self):
        """Run the sync process."""
        success = self.sync_shifts_to_calendar()
        if success:
            self.logger.info("Weekly shift sync completed successfully!")
        else:
            self.logger.error("Weekly shift sync failed!")
            sys.exit(1)


def main():
    """Main function."""
    try:
        sync_service = WeeklyShiftSync()
        sync_service.run()
    except ValueError as error:
        logging.error(f"Configuration error: {error}")
        logging.error(
            "Please check your environment variables. See .env.example for required variables."
        )
        sys.exit(1)
    except Exception as error:
        logging.error(f"Unexpected error: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
