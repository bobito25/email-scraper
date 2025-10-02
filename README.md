# Email Scraper Tool

A Python-based tool that scrapes websites to collect email addresses. Given a starting URL, this tool will recursively follow links found on the page and extract email addresses from all visited pages.

## TODOs
- Prefix tree for repeated paths -> recomendations to avoid (ex.: collections/products)
- Save page where email was found and link to it in UI

## Features
- **Recursive scraping**: Follows links on the web pages to visit multiple pages for a thorough search.
- **Email extraction**: Uses regular expressions to find and collect email addresses.
- **Live progress tracking**: The web UI streams page-processing progress and newly discovered emails in real time, so you can review results long before the crawl finishes.
- **Latency insights**: Watch live HTTP fetch and HTML parse timings (latest, average, and slowest) to pinpoint bottlenecks while a crawl is running.
- **Adaptive throttling**: Built-in rate limiting and exponential backoff keep you friendly with remote servers and avoid HTTP 429 rate-limit responses.
- **Easy to use**: Just enter a URL, and the tool will start scraping.

## Requirements
- `python 3.x`
- `requests` – For making HTTP requests.
- `beautifulsoup4` – For parsing and navigating HTML.
- `lxml` – An XML/HTML parser for BeautifulSoup.

## Installation Guide

### 1. Clone the repository:
```bash
git clone https://github.com/bobito25/email-scraper.git
cd email-scraper
```

### 2. Set up a virtual environment (optional but recommended):
```bash
python -m venv venv
source venv/bin/activate  
```
> On Windows: venv\Scripts\activate

### 3. Install dependencies:
The required libraries are listed in requirements.txt. You can install them using pip:
```bash
pip install -r requirements.txt
```
If you don't have the requirements.txt file yet, you can generate it as follows:
```bash
pip freeze > requirements.txt
```

### 4. Run the tool:
After installing the dependencies, you can run the tool by executing the following command:

```bash
python email_scraper.py
```

### Key CLI options for polite crawling

- `--min-interval`: Minimum delay (seconds) between requests shared across all worker threads. Defaults to `0.5` and can be set to `0` to disable throttling.
- `--max-retries`: Number of attempts per URL for transient failures such as `429` or `5xx` responses. Defaults to `3`.
- `--backoff-factor`: Multiplier used for exponential backoff when retrying; higher values slow the crawler more aggressively after failures. Defaults to `1.5`.

Example:

```bash
python email_scraper.py https://example.com --min-interval 1.0 --max-retries 5 --backoff-factor 2.0
```

## Example Output
```
[+] Enter url to scan: https://example.com
[1] Processing https://example.com
[2] Processing https://example.com/contact
Found emails:
info@example.com
support@example.com
```

## Web App UI

You can drive the scraper through a lightweight Flask web interface located in `web_app.py`.

### Run locally

```bash
python web_app.py
```

Visit <http://localhost:5000> and submit a domain or URL. The app:

- Normalizes your input (adds `https://` when missing).
- Scrapes the site using the same engine as the CLI, running in a background worker so the UI never blocks.
- Streams progress to the browser: watch pages processed, queue depth, and running errors while the crawl is underway.
- Surfaces detailed timing stats, showing the last, average, and slowest fetch/parse durations so you can see where the crawler is spending time.
- Validates each email via `email_validator`, showing valid and invalid addresses separately the moment they are discovered.

Need programmatic status checks? Every scrape receives a job identifier and is exposed at `GET /api/jobs/<job_id>`, returning JSON progress snapshots you can poll.

### Run with Docker

```bash
docker build -t email-scraper-web .
docker run --rm -p 5004:5004 email-scraper-web
```

Set a custom port by tweaking the `PORT` environment variable when running the container:

```bash
docker run --rm -e PORT=8080 -p 8080:8080 email-scraper-web
```

## Badges
[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](https://choosealicense.com/licenses/mit/)

## Credit to original project and base code
- [@AdrianTomin](https://www.github.com/AdrianTomin)
