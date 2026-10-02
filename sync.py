import requests
import json
import os
import sys
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build

NOTION_TOKEN = os.getenv('NOTION_TOKEN')
NOTION_DB_ID = os.getenv('NOTION_DB_ID')
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS')
CALENDAR_ID = os.getenv('CALENDAR_ID', 'primary')
NOTION_TITLE_PROPERTY = os.getenv('NOTION_TITLE_PROPERTY', 'Name')
NOTION_DATE_PROPERTY = os.getenv('NOTION_DATE_PROPERTY', 'Date')


def validate_env():
    missing = []
    for name, value in [
        ('NOTION_TOKEN', NOTION_TOKEN),
        ('NOTION_DB_ID', NOTION_DB_ID),
        ('GOOGLE_CREDENTIALS', GOOGLE_CREDENTIALS_JSON),
        ('CALENDAR_ID', CALENDAR_ID),
    ]:
        if not value:
            missing.append(name)
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)


def notion_headers():
    return {
        'Authorization': f'Bearer {NOTION_TOKEN}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json'
    }


def get_google_calendar_service():
    credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=['https://www.googleapis.com/auth/calendar']
    )
    return build('calendar', 'v3', credentials=credentials)


def get_notion_items():
    all_items = []
    next_cursor = None
    while True:
        body = {}
        if next_cursor:
            body['start_cursor'] = next_cursor
        response = requests.post(
            f'https://api.notion.com/v1/databases/{NOTION_DB_ID}/query',
            headers=notion_headers(),
            json=body
        )
        if response.status_code != 200:
            print(f"❌ Error fetching Notion data: {response.status_code}")
            print(response.text)
            return all_items
        data = response.json()
        all_items.extend(data.get('results', []))
        next_cursor = data.get('next_cursor')
        if not data.get('has_more') or not next_cursor:
            break
    return all_items


def extract_notion_title(item):
    prop = item.get('properties', {}).get(NOTION_TITLE_PROPERTY, {})
    if prop.get('type') == 'title':
        title = ''.join(x.get('plain_text', '') for x in prop.get('title', []))
        if title:
            return title
    return 'Untitled Event'


def get_page_title(page_id):
    if not page_id:
        return ''
    try:
        r = requests.get(
            f'https://api.notion.com/v1/pages/{page_id}',
            headers=notion_headers()
        )
        if r.status_code != 200:
            return ''
        for prop in r.json().get('properties', {}).values():
            if prop.get('type') == 'title':
                return ''.join(x.get('plain_text', '') for x in prop.get('title', []))
    except Exception:
        pass
    return ''


def extract_client(prop):
    if not prop:
        return ''
    t = prop.get('type')

    if t == 'relation':
        rel = prop.get('relation', [])
        if rel:
            return get_page_title(rel[0].get('id'))

    if t == 'formula':
        formula = prop.get('formula', {})
        if formula.get('type') == 'string':
            return formula.get('string') or ''
        if formula.get('type') == 'array':
            for item in formula.get('array', []):
                name = extract_client(item)
                if name:
                    return name

    if t == 'rollup':
        rollup = prop.get('rollup', {})
        if rollup.get('type') == 'array':
            for item in rollup.get('array', []):
                if item.get('type') == 'relation':
                    rel = item.get('relation', [])
                    if rel:
                        name = get_page_title(rel[0].get('id'))
                        if name:
                            return name
                if item.get('type') in ('title', 'rich_text'):
                    key = item.get('type')
                    name = ''.join(x.get('plain_text', '') for x in item.get(key, []))
                    if name:
                        return name
    return ''


def get_client_name(properties):
    for name in ('Cliente', 'Cliente visualizzato', 'Cliente appuntamento'):
        result = extract_client(properties.get(name))
        if result:
            return result
    return ''


def get_status(properties):
    prop = properties.get('Stato', {})
    if prop.get('type') == 'status' and prop.get('status'):
        return prop['status'].get('name', '')
    if prop.get('type') == 'select' and prop.get('select'):
        return prop['select'].get('name', '')
    return ''


def notion_to_calendar_event(item):
    properties = item.get('properties', {})

    # Calendar shows only active activities.
    if get_status(properties) not in ('Da fare', 'In corso'):
        return None

    date_prop = properties.get(NOTION_DATE_PROPERTY, {})
    if date_prop.get('type') != 'date' or not date_prop.get('date'):
        return None

    start_value = date_prop['date'].get('start')
    if not start_value:
        return None

    # Ignore the time completely: every activity becomes an all-day event.
    day = start_value[:10]
    try:
        day_obj = datetime.strptime(day, '%Y-%m-%d').date()
    except ValueError:
        return None

    # Keep only today and future activities.
    if day_obj < datetime.now().date():
        return None

    end_day = (day_obj + timedelta(days=1)).strftime('%Y-%m-%d')
    client = get_client_name(properties)

    return {
        'summary': extract_notion_title(item),
        'description': f"Cliente: {client}\n\nNotion: {item.get('url', '')}",
        'start': {'date': day},
        'end': {'date': end_day}
    }


def sync_notion_to_calendar(service, notion_items):
    created = updated = skipped = deleted = 0
    active_ids = set()

    print("🔄 Syncing Notion → Google Calendar...")

    for item in notion_items:
        try:
            event = notion_to_calendar_event(item)
            if not event:
                skipped += 1
                continue

            notion_id = item['id']
            active_ids.add(notion_id)
            event['extendedProperties'] = {'private': {'notion_id': notion_id}}

            existing = service.events().list(
                calendarId=CALENDAR_ID,
                privateExtendedProperty=f'notion_id={notion_id}'
            ).execute().get('items', [])

            if existing:
                old = existing[0]
                same = (
                    old.get('summary', '') == event.get('summary', '') and
                    old.get('description', '') == event.get('description', '') and
                    old.get('start', {}) == event.get('start', {}) and
                    old.get('end', {}) == event.get('end', {})
                )
                if same:
                    print(f"⏭️ No changes for: {event['summary']}")
                    skipped += 1
                    continue

                service.events().update(
                    calendarId=CALENDAR_ID,
                    eventId=old['id'],
                    body=event
                ).execute()
                print(f"🔄 Updated calendar event: {event['summary']}")
                updated += 1
            else:
                service.events().insert(
                    calendarId=CALENDAR_ID,
                    body=event
                ).execute()
                print(f"✅ Created calendar event: {event['summary']}")
                created += 1

        except Exception as e:
            print(f"❌ Error syncing item to calendar: {e}")

    # Delete previously-synced events that are no longer active.
    # This includes activities changed to "Fatto".
    try:
        print("🔍 Checking for calendar events to delete...")
        page_token = None
        synced = []

        while True:
            result = service.events().list(
                calendarId=CALENDAR_ID,
                maxResults=2500,
                pageToken=page_token
            ).execute()

            for event in result.get('items', []):
                private = event.get('extendedProperties', {}).get('private', {})
                if 'notion_id' in private:
                    synced.append(event)

            page_token = result.get('nextPageToken')
            if not page_token:
                break

        print(f"🔍 Found {len(synced)} previously synced events")

        for event in synced:
            notion_id = event.get('extendedProperties', {}).get('private', {}).get('notion_id')
            if notion_id and notion_id not in active_ids:
                service.events().delete(
                    calendarId=CALENDAR_ID,
                    eventId=event['id']
                ).execute()
                print(f"🗑️ Deleted calendar event: {event.get('summary', 'Untitled')}")
                deleted += 1

    except Exception as e:
        print(f"❌ Error during calendar deletion sync: {e}")

    return created, updated, skipped, deleted


def main():
    print("🔄 Starting Notion → Google Calendar sync...")
    validate_env()

    try:
        service = get_google_calendar_service()
        print("🔗 Connected to Google Calendar")
    except Exception as e:
        print(f"❌ Failed to connect to Google Calendar: {e}")
        return

    items = get_notion_items()
    print(f"📋 Found {len(items)} Notion items")

    created, updated, skipped, deleted = sync_notion_to_calendar(service, items)

    print(f"""
🎉 Notion → Google Calendar Sync Complete!

Notion → Calendar:
  Created: {created}
  Updated: {updated}
  Skipped: {skipped}
  Deleted: {deleted}
""")


if __name__ == '__main__':
    main()
