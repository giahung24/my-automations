"""
Google Calendar API wrapper for various calendar operations.

Authentication uses a Google service account (JSON key file). The target
calendar must be shared with the service account's email address with the
"Make changes to events" permission.
"""

import json
import os
from datetime import datetime, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build


class GoogleCalendarAPI:
    SCOPES = ['https://www.googleapis.com/auth/calendar']

    def __init__(self, credentials_file='credentials.json', credentials_info=None):
        """Initialize the Google Calendar API.

        Args:
            credentials_file: Path to the service account JSON key file.
            credentials_info: Optional dict (already-parsed service account JSON).
                When provided, it takes precedence over ``credentials_file``.
        """
        self.credentials_file = credentials_file
        self.credentials_info = credentials_info
        self.service = None
        self.authenticate()

    def authenticate(self):
        """Authenticate with the service account and create the calendar service"""
        if self.credentials_info:
            creds = service_account.Credentials.from_service_account_info(
                self.credentials_info, scopes=self.SCOPES)
        else:
            if not os.path.exists(self.credentials_file):
                raise FileNotFoundError(
                    f"Service account key file not found: {self.credentials_file}")
            creds = service_account.Credentials.from_service_account_file(
                self.credentials_file, scopes=self.SCOPES)

        self.service = build('calendar', 'v3', credentials=creds, cache_discovery=False)

    @property
    def service_account_email(self):
        """Email of the service account (share your calendar with this address)."""
        if self.credentials_info:
            return self.credentials_info.get('client_email')
        with open(self.credentials_file) as f:
            return json.load(f).get('client_email')

    def create_calendar(self, summary, description="", timezone="Europe/Paris", location=""):
        """Create a new calendar"""
        try:
            calendar = {
                'summary': summary,
                'description': description,
                'timeZone': timezone,
                'location': location
            }

            created_calendar = self.service.calendars().insert(body=calendar).execute()

            print(f'Calendar created successfully!')
            print(f'- Name: {created_calendar["summary"]}')
            print(f'- ID: {created_calendar["id"]}')

            return created_calendar
        except Exception as error:
            print(f'An error occurred while creating calendar: {error}')
            return None

    def list_calendars(self):
        """List all calendars"""
        try:
            calendars_result = self.service.calendarList().list().execute()
            calendars = calendars_result.get('items', [])

            print('Available Calendars:')
            for calendar in calendars:
                print(f"- {calendar['summary']} (ID: {calendar['id']})")

            return calendars
        except Exception as error:
            print(f'An error occurred: {error}')
            return None

    def get_events(self, calendar_id='primary', max_results=10, time_min=None, time_max=None):
        """Get events from a calendar"""
        try:
            if time_min is None:
                time_min = datetime.utcnow().isoformat() + 'Z'

            events_result = self.service.events().list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max_results,
                singleEvents=True,
                orderBy='startTime'
            ).execute()

            events = events_result.get('items', [])

            if not events:
                print('No upcoming events found.')
                return []

            print(f'Upcoming {len(events)} events:')
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                print(f"- {event['summary']} ({start})")

            return events
        except Exception as error:
            print(f'An error occurred: {error}')
            return None

    def get_events_in_range(self, calendar_id, start_date, end_date):
        """Get events in a specific date range"""
        try:
            events_result = self.service.events().list(
                calendarId=calendar_id,
                timeMin=start_date.isoformat(),
                timeMax=end_date.isoformat(),
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            return events_result.get('items', [])
        except Exception as error:
            print(f'Error getting events in range: {error}')
            return []

    def create_event(self, summary, start_time, end_time, description='', location='',
                    calendar_id='primary', timezone='UTC', color_id=None,
                    extended_properties=None):
        """Create a new event"""
        try:
            event = {
                'summary': summary,
                'location': location,
                'description': description,
                'start': {
                    'dateTime': start_time.isoformat(),
                    'timeZone': timezone,
                },
                'end': {
                    'dateTime': end_time.isoformat(),
                    'timeZone': timezone,
                },
            }
            
            # Add color if specified
            if color_id:
                event['colorId'] = color_id
            
            # Add extended properties for metadata
            if extended_properties:
                event['extendedProperties'] = extended_properties

            created_event = self.service.events().insert(
                calendarId=calendar_id, 
                body=event
            ).execute()

            print(f'Event created: {created_event.get("htmlLink", created_event.get("id"))}')
            return created_event
        except Exception as error:
            print(f'An error occurred creating event: {error}')
            return None

    def update_event(self, event_id, calendar_id='primary', **kwargs):
        """Update an existing event"""
        try:
            # Get the existing event
            event = self.service.events().get(
                calendarId=calendar_id, 
                eventId=event_id
            ).execute()

            # Update fields
            for key, value in kwargs.items():
                if key in ['summary', 'description', 'location']:
                    event[key] = value
                elif key == 'start_time':
                    event['start']['dateTime'] = value.isoformat()
                elif key == 'end_time':
                    event['end']['dateTime'] = value.isoformat()

            updated_event = self.service.events().update(
                calendarId=calendar_id,
                eventId=event_id,
                body=event
            ).execute()

            print(f'Event updated: {updated_event.get("htmlLink")}')
            return updated_event
        except Exception as error:
            print(f'An error occurred updating event: {error}')
            return None

    def delete_event(self, event_id, calendar_id='primary'):
        """Delete an event"""
        try:
            self.service.events().delete(
                calendarId=calendar_id,
                eventId=event_id
            ).execute()

            print(f'Event deleted successfully: {event_id}')
            return True
        except Exception as error:
            print(f'Error deleting event {event_id}: {error}')
            return False

    def search_events(self, query, calendar_id='primary', max_results=10):
        """Search for events"""
        try:
            events_result = self.service.events().list(
                calendarId=calendar_id,
                q=query,
                maxResults=max_results,
                singleEvents=True,
                orderBy='startTime'
            ).execute()

            events = events_result.get('items', [])

            print(f'Found {len(events)} events matching "{query}":')
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                print(f"- {event['summary']} ({start})")

            return events
        except Exception as error:
            print(f'An error occurred searching events: {error}')
            return None

    def get_events_for_date(self, events, target_date):
        """Filter events that occur on a specific date"""
        events_on_date = []
        target_date_str = target_date.strftime('%Y-%m-%d')
        
        for event in events:
            event_start = event['start'].get('dateTime', event['start'].get('date'))
            if event_start.startswith(target_date_str):
                events_on_date.append(event)
        
        return events_on_date

    def delete_events_on_date(self, calendar_id, target_date, events=None):
        """Delete all events on a specific date"""
        if events is None:
            # Get events for the day
            start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end_of_day = start_of_day + timedelta(days=1)
            events = self.get_events_in_range(calendar_id, start_of_day, end_of_day)
        
        events_on_date = self.get_events_for_date(events, target_date)
        
        deleted_count = 0
        for event in events_on_date:
            if self.delete_event(event['id'], calendar_id):
                deleted_count += 1
        
        return deleted_count