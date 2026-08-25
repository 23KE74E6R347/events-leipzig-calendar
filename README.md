# Events Leipzig Calendar

A small, dependency-light GitHub Pages calendar aggregator for Leipzig.

It combines:

_PLANLOS events
_Sachsenpunk events located in Leipzig
_Sachsenpunk location pages for address enrichment

The generated 'events-leipzig.ics' is suitable as an iOS/macOS calendar subscription.

## Architecture

The repository uses GitHub Actions to refresh the feed
automatically. The Pages site serves the most recently generated feed.

The workflow runs:

_on every push to 'main'
_manually via 'workflow_dispatch'
_every 6 hours

GitHub Pages serves:

_'index.html'
_'events-leipzig.ics'

## GitHub Pages setup

Repository must be public for GitHub Free Pages.

In **Settings** → **Pages** choose:

_Source: **Deploy from a branch**
- Branch: **main**
- Folder: **/ (root)**

After Pages is enabled, the subscription URL is:

'https://<username>.github.io/events-leipzig-calendar/events-leipzig.ics'

Replace the username in the URL with your own handle.

## iOS

**Calendar** → **Calendars** → **+** → **Add Subscription Calendar**

Paste the '.ics' URL and name it to use the calendar.

## Important limitation

A static GitHub Pages request cannot itself execute Python or fetch arbitrary third-party
websites. The automatic refresh is therefore done by GitHub Actions, not by the browser
page-load. The HTML page always loads the latest generated feed and shows its generation
timestamp.

This is more reliable for an actual iOS calendar subscription than a browser-side CORS
proxy.