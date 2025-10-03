from collections import deque
import concurrent.futures
import urllib.parse
import re
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Literal, Optional

from bs4 import BeautifulSoup
import requests
import requests.exceptions as request_exception
import threading
from time import perf_counter, sleep


def get_base_url(url: str) -> str:
    """
    Extracts the base URL (scheme and netloc) from a given URL.

    :param url: The full URL from which to extract the base.
    :return: The base URL in the form 'scheme://netloc'.
    """

    parts = urllib.parse.urlsplit(url)
    return '{0.scheme}://{0.netloc}'.format(parts)


def extract_emails(response_text: str) -> set[str]:
    """
    Extracts all email addresses from the provided HTML text.

    :param response_text: The raw HTML content of a webpage.
    :return: A set of email addresses found within the content.
    """

    email_pattern = r'[a-z0-9\.\-+]+@[a-z0-9\.\-+]+\.[a-z]+'
    return set(re.findall(email_pattern, response_text, re.I))


ScrapeStatus = Literal['started', 'running', 'error', 'finished', 'cancelled']


class ScrapeCancelled(Exception):
    """Raised when the scraping process is cancelled by the caller."""

    pass


@dataclass(slots=True, frozen=True)
class PathComponentSummary:
    url_count: int
    email_count: int


@dataclass(slots=True)
class ScrapeProgress:
    status: ScrapeStatus
    processed_count: int
    max_count: int
    queued_count: int
    unique_emails_found: int
    current_url: Optional[str] = None
    new_emails: tuple[str, ...] = ()
    message: Optional[str] = None
    last_fetch_time: Optional[float] = None
    last_parse_time: Optional[float] = None
    avg_fetch_time: Optional[float] = None
    avg_parse_time: Optional[float] = None
    max_fetch_time: Optional[float] = None
    max_parse_time: Optional[float] = None
    min_fetch_time: Optional[float] = None
    min_parse_time: Optional[float] = None
    total_fetch_time: float = 0.0
    total_parse_time: float = 0.0
    fetch_sample_count: int = 0
    parse_sample_count: int = 0
    path_component_stats: dict[str, PathComponentSummary] = field(default_factory=dict)


ProgressCallback = Callable[[ScrapeProgress], None]


@dataclass(slots=True)
class ScrapeResult:
    emails: set[str]
    path_component_stats: dict[str, PathComponentSummary]


class RateLimiter:
    """A simple thread-safe rate limiter enforcing a minimum interval between requests."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next_allowed_time = 0.0

    def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        while True:
            with self._lock:
                now = perf_counter()
                wait = self._next_allowed_time - now
                if wait <= 0:
                    self._next_allowed_time = max(self._next_allowed_time, now) + self.min_interval
                    return
            sleep(wait)

    def backoff(self, extra_wait: float) -> None:
        if extra_wait <= 0:
            return
        with self._lock:
            target_time = perf_counter() + extra_wait
            if target_time > self._next_allowed_time:
                self._next_allowed_time = target_time


def _parse_retry_after(raw_value: str) -> Optional[float]:
    if not raw_value:
        return None
    try:
        seconds = float(raw_value)
        if seconds >= 0:
            return seconds
    except ValueError:
        pass
    try:
        retry_time = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError):
        return None
    if retry_time is None:
        return None
    if retry_time.tzinfo is None:
        retry_time = retry_time.replace(tzinfo=timezone.utc)
    wait_seconds = (retry_time - datetime.now(timezone.utc)).total_seconds()
    return wait_seconds if wait_seconds >= 0 else 0.0


def _calculate_retry_wait(response: Optional[requests.Response], attempt: int, backoff_factor: float) -> float:
    wait_time: Optional[float] = None
    if response is not None:
        retry_after = response.headers.get('Retry-After')
        if retry_after:
            wait_time = _parse_retry_after(retry_after)
    if wait_time is None:
        wait_time = backoff_factor * (2 ** max(0, attempt - 1))
    return min(60.0, max(1.0, wait_time))


def scrape_website(start_url: str, max_count: int = 100, stay_in_domain: bool = True,
                   exclude_strs: list[str] = None, timeout: float = 10.0,
                   output_file: str | None = None, verbose: bool = False,
                   progress_callback: Optional[ProgressCallback] = None,
                   min_request_interval: float = 0.5, max_retries: int = 3,
                   backoff_factor: float = 1.5,
                   cancellation_event: Optional[threading.Event] = None,
                   concurrency: int = 1) -> ScrapeResult:
    """
    Scrapes a website starting from the given URL, follows links, and collects email addresses.
    Optionally appends newly found (deduplicated) emails to an output file in a thread-safe way.
    Added: when verbose=True prints per-URL fetch and parse times.
    :param start_url: The URL to start scraping from.
    :param max_count: The maximum number of pages to scrape (default: 100).
    :param stay_in_domain: Whether to restrict scraping to the starting domain (default: True).
    :param exclude_strs: List of substrings; URLs containing any of these will be excluded (default: None).
    :param timeout: The timeout in seconds for each HTTP request (default: 10.0).
    :param output_file: Path to file where new emails are appended (default: None -> no file writing).
    :param verbose: Print timing info per processed URL (default: False).
    :param progress_callback: Optional callback receiving progress updates.
    :param min_request_interval: Minimum delay (seconds) between HTTP requests across threads (default: 0.5).
    :param max_retries: Maximum retries for transient errors including HTTP 429 (default: 3).
    :param backoff_factor: Multiplier used when calculating exponential backoff delays (default: 1.5).
    :param cancellation_event: Optional event used to cancel the scraping process.
    :param concurrency: Number of concurrent fetch workers (1 = sequential, default).
    :return: A ScrapeResult containing the unique email addresses found and
        aggregated statistics for each path component encountered.
    """
    urls_to_process = deque([start_url])
    scraped_urls = set()
    collected_emails = set()
    count = 0
    component_stats: dict[str, dict[str, int]] = {}
    # thread-safe email recording
    email_lock = threading.Lock()
    written_emails = set()
    if output_file:
        # touch file
        open(output_file, 'a', encoding='utf-8').close()

    # Add session for connection pooling
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; EmailScraper/1.0)'})

    min_request_interval = max(0.0, float(min_request_interval))
    rate_limiter = RateLimiter(min_request_interval) if min_request_interval > 0 else None
    max_retries = max(1, int(max_retries))
    backoff_factor = max(0.1, float(backoff_factor))
    concurrency = max(1, int(concurrency))

    last_fetch_time: Optional[float] = None
    last_parse_time: Optional[float] = None
    total_fetch_time = 0.0
    total_parse_time = 0.0
    fetch_sample_count = 0
    parse_sample_count = 0
    max_fetch_time: Optional[float] = None
    max_parse_time: Optional[float] = None
    min_fetch_time: Optional[float] = None
    min_parse_time: Optional[float] = None

    cancelled_notified = False

    def snapshot_component_stats() -> dict[str, PathComponentSummary]:
        if not component_stats:
            return {}
        return {
            component: PathComponentSummary(
                url_count=stats['url_count'],
                email_count=stats['email_count'],
            )
            for component, stats in component_stats.items()
        }

    def emit_progress(status: ScrapeStatus, *, current_url: Optional[str], new_emails: Optional[set[str]] = None,
                      message: Optional[str] = None):
        if not progress_callback:
            return
        new_email_tuple: tuple[str, ...] = ()
        if new_emails:
            new_email_tuple = tuple(sorted(new_emails))
        progress_callback(
            ScrapeProgress(
                status=status,
                processed_count=count,
                max_count=max_count,
                queued_count=len(urls_to_process),
                unique_emails_found=len(collected_emails),
                current_url=current_url,
                new_emails=new_email_tuple,
                message=message,
                last_fetch_time=last_fetch_time,
                last_parse_time=last_parse_time,
                avg_fetch_time=(total_fetch_time / fetch_sample_count) if fetch_sample_count else None,
                avg_parse_time=(total_parse_time / parse_sample_count) if parse_sample_count else None,
                max_fetch_time=max_fetch_time if fetch_sample_count else None,
                max_parse_time=max_parse_time if parse_sample_count else None,
                min_fetch_time=min_fetch_time,
                min_parse_time=min_parse_time,
                total_fetch_time=total_fetch_time,
                total_parse_time=total_parse_time,
                fetch_sample_count=fetch_sample_count,
                parse_sample_count=parse_sample_count,
                path_component_stats=snapshot_component_stats(),
            )
        )

    def notify_cancelled(current_url: Optional[str] = None, message: Optional[str] = "Scrape cancelled by user") -> None:
        nonlocal cancelled_notified
        if cancelled_notified:
            return
        cancelled_notified = True
        emit_progress('cancelled', current_url=current_url, new_emails=set(), message=message)

    def check_cancelled(current_url: Optional[str] = None) -> None:
        if cancellation_event is not None and cancellation_event.is_set():
            notify_cancelled(current_url=current_url)
            raise ScrapeCancelled("Scrape cancelled by caller")

    if progress_callback:
        emit_progress('started', current_url=None)

    try:
        check_cancelled()

        def handle_result(url: str, emails: set[str], links: list[str],
                          fetch_time: float, parse_time: float):
            nonlocal last_fetch_time, last_parse_time, total_fetch_time, total_parse_time
            nonlocal fetch_sample_count, parse_sample_count
            nonlocal max_fetch_time, max_parse_time, min_fetch_time, min_parse_time
            # verbose timings
            if verbose:
                print(f'    Fetch: {fetch_time:.3f}s Parse: {parse_time:.3f}s')
            newly_discovered = set()
            if emails:
                newly_discovered = emails - collected_emails
                new_emails_for_file = newly_discovered - written_emails
                if new_emails_for_file and output_file:
                    with email_lock:
                        really_new = new_emails_for_file - written_emails
                        if really_new:
                            with open(output_file, 'a', encoding='utf-8') as f:
                                for e in sorted(really_new):
                                    f.write(e + '\n')
                            written_emails.update(really_new)
                            print(f'[+] Appended {len(really_new)} new emails to {output_file}')
            collected_emails.update(emails)
            base_url = get_base_url(url)
            for link in links:
                if stay_in_domain and not link.startswith(base_url):
                    continue
                if exclude_strs and any(excl in link for excl in exclude_strs):
                    continue
                if link not in scraped_urls and link not in urls_to_process:
                    urls_to_process.append(link)
            # metrics
            last_fetch_time = fetch_time
            last_parse_time = parse_time
            total_fetch_time += fetch_time
            total_parse_time += parse_time
            fetch_sample_count += 1
            parse_sample_count += 1
            max_fetch_time = fetch_time if max_fetch_time is None else max(max_fetch_time, fetch_time)
            max_parse_time = parse_time if max_parse_time is None else max(max_parse_time, parse_time)
            min_fetch_time = fetch_time if min_fetch_time is None else min(min_fetch_time, fetch_time)
            min_parse_time = parse_time if min_parse_time is None else min(min_parse_time, parse_time)
            # update path component stats
            parsed_url = urllib.parse.urlsplit(url)
            components = [segment for segment in parsed_url.path.split('/') if segment]
            if components:
                email_count_for_url = len(emails)
                for component in set(components):
                    stats_entry = component_stats.setdefault(component, {'url_count': 0, 'email_count': 0})
                    stats_entry['url_count'] += 1
                    stats_entry['email_count'] += email_count_for_url
            emit_progress('running', current_url=url, new_emails=newly_discovered)

        while urls_to_process and count < max_count:
            check_cancelled()
            if concurrency == 1:
                # SEQUENTIAL path
                url = urls_to_process.popleft()
                if url in scraped_urls:
                    continue
                scraped_urls.add(url)
                count += 1
                print(f'[{count}] Processing {url}')
                try:
                    emails, links, fetch_time, parse_time = process_single_url(
                        session,
                        url,
                        timeout=timeout,
                        limiter=rate_limiter,
                        max_retries=max_retries,
                        backoff_factor=backoff_factor,
                        cancellation_event=cancellation_event,
                    )
                    check_cancelled(current_url=url)
                except ScrapeCancelled:
                    notify_cancelled(current_url=url)
                    raise
                except Exception as e:
                    print(f'Error processing {url}: {e}')
                    emit_progress('error', current_url=url, new_emails=set(), message=str(e))
                    continue
                handle_result(url, emails, links, fetch_time, parse_time)
                continue  # next loop iteration
            # CONCURRENT path
            batch_size = min(concurrency, len(urls_to_process), max_count - count)
            current_batch = []
            for _ in range(batch_size):
                if not urls_to_process:
                    break
                next_url = urls_to_process.popleft()
                if next_url not in scraped_urls:
                    current_batch.append(next_url)
                    scraped_urls.add(next_url)
            if not current_batch:
                continue
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                future_to_url = {
                    executor.submit(
                        process_single_url,
                        session,
                        url,
                        timeout=timeout,
                        limiter=rate_limiter,
                        max_retries=max_retries,
                        backoff_factor=backoff_factor,
                        cancellation_event=cancellation_event,
                    ): url
                    for url in current_batch
                }
                for future in concurrent.futures.as_completed(future_to_url):
                    url = future_to_url[future]
                    count += 1
                    print(f'[{count}] Processing {url}')
                    try:
                        check_cancelled(current_url=url)
                        emails, links, fetch_time, parse_time = future.result(timeout=10)
                        check_cancelled(current_url=url)
                    except ScrapeCancelled:
                        notify_cancelled(current_url=url)
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise
                    except Exception as e:
                        print(f'Error processing {url}: {e}')
                        emit_progress('error', current_url=url, new_emails=set(), message=str(e))
                        continue
                    handle_result(url, emails, links, fetch_time, parse_time)
        if progress_callback and not cancelled_notified:
            emit_progress('finished', current_url=None)
        return ScrapeResult(
            emails=collected_emails,
            path_component_stats=snapshot_component_stats(),
        )
    except ScrapeCancelled:
        raise
    finally:
        session.close()


def process_single_url(session, url, timeout=10.0, limiter: Optional[RateLimiter] = None,
                       max_retries: int = 3, backoff_factor: float = 1.5,
                       cancellation_event: Optional[threading.Event] = None):
    """
    Process a single URL and return emails and links found plus timing.
    :return: (emails_set, links_list, fetch_time, parse_time)
    """
    fetch_time = 0.0
    parse_time = 0.0
    attempt = 0

    def check_cancelled():
        if cancellation_event is not None and cancellation_event.is_set():
            raise ScrapeCancelled("Scrape cancelled by caller")

    def wait_with_cancel(delay: float) -> None:
        if delay <= 0:
            return
        if cancellation_event is not None:
            if cancellation_event.wait(delay):
                raise ScrapeCancelled("Scrape cancelled by caller")
            return
        sleep(delay)

    while attempt < max_retries:
        check_cancelled()
        attempt += 1
        if limiter:
            limiter.acquire()

        t0 = perf_counter()
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            fetch_time = perf_counter() - t0
            response.raise_for_status()
            check_cancelled()
        except request_exception.HTTPError as exc:
            fetch_time = perf_counter() - t0
            status_code = exc.response.status_code if exc.response is not None else 'unknown'
            reason = exc.response.reason if exc.response is not None else str(exc)
            if status_code == 429 and attempt < max_retries:
                wait_time = _calculate_retry_wait(exc.response, attempt, backoff_factor)
                if limiter:
                    limiter.backoff(wait_time)
                wait_with_cancel(wait_time)
                continue
            if status_code in {500, 502, 503, 504} and attempt < max_retries:
                wait_time = min(60.0, max(1.0, backoff_factor * (2 ** max(0, attempt - 1))))
                if limiter:
                    limiter.backoff(wait_time)
                wait_with_cancel(wait_time)
                continue
            raise RuntimeError(f"HTTP {status_code} error while fetching {url}: {reason}") from exc
        except request_exception.RequestException as exc:
            fetch_time = perf_counter() - t0
            if attempt < max_retries:
                wait_time = min(60.0, max(1.0, backoff_factor * (2 ** max(0, attempt - 1))))
                if limiter:
                    limiter.backoff(wait_time)
                wait_with_cancel(wait_time)
                continue
            raise RuntimeError(f"Request error while fetching {url}: {exc}") from exc

        content_type = response.headers.get('content-type', '').lower()
        if 'text/html' not in content_type:
            return set(), [], fetch_time, 0.0

        t1 = perf_counter()
        emails = extract_emails(response.text)
        soup = BeautifulSoup(response.text, 'lxml')
        links = []
        for anchor in soup.find_all('a', href=True):
            link = anchor['href'].strip()
            if not link:
                continue
            lower_link = link.lower()
            if lower_link.startswith(('javascript:', 'mailto:', '#')):
                continue
            if any(ext in lower_link for ext in ['.pdf', '.jpg', '.png', '.gif', '.zip', '.doc']):
                continue
            normalized_link = urllib.parse.urljoin(url, link)
            if normalized_link:
                links.append(normalized_link)
        parse_time = perf_counter() - t1
        check_cancelled()
        return emails, links, fetch_time, parse_time

    raise RuntimeError(f"Exceeded retry limit while fetching {url}")


def main():
    """
    Main function to handle CLI arguments and run the email scraper.

    This function sets up command-line argument parsing, processes user input,
    and orchestrates the email scraping process. It handles URL normalization,
    validates arguments, and displays results to the user.

    :return: None. Outputs results to stdout and exits with appropriate status.
    """
    parser = argparse.ArgumentParser(
        description='Scrape websites for email addresses',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''Examples:
  python email_scraper.py https://example.com
  python email_scraper.py https://example.com --exclude admin contact support
  python email_scraper.py example.com --max-count 50 --exclude .pdf .jpg'''
    )

    parser.add_argument('url', help='URL to start scraping from')
    parser.add_argument('--exclude', nargs='*', default=[],
                       help='Strings to exclude from URLs (e.g., admin contact .pdf)')
    parser.add_argument('--max-count', type=int, default=100,
                       help='Maximum number of pages to scrape (default: 100)')
    parser.add_argument('--allow-external', action='store_true',
                       help='Allow scraping external domains')
    parser.add_argument('--timeout', type=float, default=10.0,
                       help='Request timeout in seconds (default: 10.0)')
    parser.add_argument('--output', default='emails.txt',
                       help='File to append found emails (default: emails.txt)')
    parser.add_argument('--verbose', action='store_true',
                        help='Print per-request fetch/parse timing')
    parser.add_argument('--min-interval', type=float, default=0.5,
                        help='Minimum delay between HTTP requests in seconds (default: 0.5, set to 0 to disable)')
    parser.add_argument('--max-retries', type=int, default=3,
                        help='Maximum retries for transient HTTP errors such as 429 (default: 3)')
    parser.add_argument('--backoff-factor', type=float, default=1.5,
                        help='Multiplier used for exponential backoff when retrying (default: 1.5)')
    parser.add_argument('--concurrency', type=int, default=1,
                        help='Number of concurrent workers (default: 1 = sequential)')

    args = parser.parse_args()

    # Process URL - add https if not present
    user_url = args.url
    if not user_url.startswith(('http://', 'https://')):
        user_url = 'https://' + user_url

    # Handle stay_in_domain logic
    stay_in_domain = not args.allow_external
    min_interval = max(0.0, args.min_interval)
    max_retries = max(1, args.max_retries)
    backoff_factor = max(0.1, args.backoff_factor)

    print(f'[+] Starting scrape of: {user_url}')
    if args.exclude:
        print(f'[+] Excluding URLs containing: {", ".join(args.exclude)}')
    print(f'[+] Max pages to scrape: {args.max_count}')
    print(f'[+] Stay in domain: {stay_in_domain}')
    print(f'[+] Request timeout: {args.timeout} seconds')
    print(f'[+] Min interval between requests: {min_interval:.2f} seconds')
    print(f'[+] Max retries per URL: {max_retries}')
    print(f'[+] Backoff factor: {backoff_factor:.2f}')
    print(f'[+] Concurrency: {args.concurrency} ({"sequential" if args.concurrency <= 1 else "parallel"})')
    print(f'[+] Emails will be appended to: {args.output}\n')

    try:
        result = scrape_website(
            user_url,
            max_count=args.max_count,
            stay_in_domain=stay_in_domain,
            exclude_strs=args.exclude if args.exclude else None,
            timeout=args.timeout,
            output_file=args.output,
            verbose=args.verbose,
            min_request_interval=min_interval,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            concurrency=args.concurrency,
        )
        emails = result.emails
        component_stats = result.path_component_stats

        print('\n[+] Finished scraping!')

    except KeyboardInterrupt:
        print('\n[-] Interrupted by user!')
        return

    # Display collected emails
    if emails:
        print('\n[+] Found emails:')
        for email in sorted(emails):
            print(email)
        print(f'\n[+] Total emails found: {len(emails)}')
    else:
        print('[-] No emails found.')

    if component_stats:
        print('\n[+] Path component stats (top 10 by pages scanned):')
        top_components = sorted(
            component_stats.items(),
            key=lambda item: item[1].url_count,
            reverse=True,
        )[:10]
        for component, stats in top_components:
            print(f'    {component}: {stats.url_count} pages, {stats.email_count} emails')
        zero_email_components = [
            component for component, stats in top_components if stats.email_count == 0
        ]
        if zero_email_components:
            print('    Components with zero collected emails in this sample:')
            for component in zero_email_components:
                print(f'        - {component}')


if __name__ == '__main__':
    main()
