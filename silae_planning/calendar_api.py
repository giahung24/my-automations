"""
Google Calendar API wrapper for various calendar operations.
"""

import os
import pickle
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from datetime import datetime, timedelta


class GoogleCalendarAPI:
    def __init__(self, credentials_file='credentials.json', token_file='token.pickle'):
        """Initialize the Google Calendar API"""
        # Define the scopes
        self.SCOPES = ['https://www.googleapis.com/auth/calendar']
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = None
        self.authenticate()

    def authenticate(self):
        """Authenticate and create the calendar service"""
        creds = None

        # Load existing token if it exists
        if os.path.exists(self.token_file):
            with open(self.token_file, 'rb') as token:
                creds = pickle.load(token)

        # If there are no valid credentials, request authorization
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, self.SCOPES)
                creds = flow.run_local_server(port=0)

            # Save credentials for next run
            with open(self.token_file, 'wb') as token:
                pickle.dump(creds, token)

        self.service = build('calendar', 'v3', credentials=creds)

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
                    reminders=None, extended_properties=None):
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
            
            # Add reminders
            if reminders:
                event['reminders'] = reminders
            else:
                event['reminders'] = {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'email', 'minutes': 24 * 60},
                        {'method': 'popup', 'minutes': 10},
                    ],
                }
            
            # Add extended properties for metadata
            if extended_properties:
                event['extendedProperties'] = extended_properties

            created_event = self.service.events().insert(
                calendarId=calendar_id, 
                body=event
            ).execute()

            print(f'Event created: {event.get("htmlLink", created_event.get("id"))}')
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