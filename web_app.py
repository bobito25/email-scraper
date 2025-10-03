from __future__ import annotations

import os
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from clean_emails import filter_valid_emails
from email_scraper import ScrapeCancelled, ScrapeProgress, scrape_website


app = Flask(__name__)


def parse_exclude_terms(raw_exclude: str) -> Optional[list[str]]:
	tokens = [token.strip() for token in re.split(r"[\s,]+", raw_exclude) if token.strip()]
	return tokens or None


def normalize_url(user_input: str) -> str:
	user_input = user_input.strip()
	if not user_input:
		return user_input
	if not user_input.startswith(("http://", "https://")):
		return f"https://{user_input}"
	return user_input


def serialize_component_stats(raw_stats: dict[str, Any]) -> dict[str, dict[str, int]]:
	serialized: dict[str, dict[str, int]] = {}
	for component, summary in raw_stats.items():
		url_count = getattr(summary, "url_count", None)
		email_count = getattr(summary, "email_count", None)
		if url_count is None and isinstance(summary, dict):
			url_count = summary.get("url_count", 0)
		if email_count is None and isinstance(summary, dict):
			email_count = summary.get("email_count", 0)
		serialized[component] = {
			"url_count": int(url_count or 0),
			"email_count": int(email_count or 0),
		}
	return serialized


@dataclass(slots=True)
class JobError:
	message: str
	url: Optional[str] = None
	timestamp: float = field(default_factory=lambda: time.time())

	def to_dict(self) -> dict[str, Any]:
		return {
			"message": self.message,
			"url": self.url,
			"timestamp": self.timestamp,
		}


@dataclass
class ScrapeJob:
	id: str
	source_url: str
	max_pages: int
	stay_in_domain: bool
	exclude_terms: Optional[list[str]]
	timeout: float
	concurrency: int
	form_values: dict[str, Any]
	status: str = "pending"
	processed_count: int = 0
	queued_count: int = 0
	unique_emails: int = 0
	current_url: Optional[str] = None
	errors: list[JobError] = field(default_factory=list)
	raw_emails: set[str] = field(default_factory=set, repr=False)
	valid_emails: set[str] = field(default_factory=set, repr=False)
	invalid_emails: set[str] = field(default_factory=set, repr=False)
	email_sources: dict[str, set[str]] = field(default_factory=dict, repr=False)
	path_component_stats: dict[str, dict[str, int]] = field(default_factory=dict)
	started_at: Optional[float] = None
	finished_at: Optional[float] = None
	last_update: Optional[float] = None
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
	cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
	worker: Optional[threading.Thread] = field(default=None, repr=False)
	_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

	def apply_progress(self, progress: ScrapeProgress) -> None:
		now = time.time()
		with self._lock:
			if progress.status == "started":
				self.status = "running"
				self.started_at = now
			if progress.status == "finished" and self.status not in {"failed", "cancelled"}:
				self.status = "finished"
			self.processed_count = progress.processed_count
			self.max_pages = progress.max_count
			self.queued_count = progress.queued_count
			self.unique_emails = progress.unique_emails_found
			self.current_url = progress.current_url
			self.last_update = now
			self.last_fetch_time = progress.last_fetch_time
			self.last_parse_time = progress.last_parse_time
			self.avg_fetch_time = progress.avg_fetch_time
			self.avg_parse_time = progress.avg_parse_time
			self.max_fetch_time = progress.max_fetch_time
			self.max_parse_time = progress.max_parse_time
			self.min_fetch_time = progress.min_fetch_time
			self.min_parse_time = progress.min_parse_time
			self.total_fetch_time = progress.total_fetch_time
			self.total_parse_time = progress.total_parse_time
			self.fetch_sample_count = progress.fetch_sample_count
			self.parse_sample_count = progress.parse_sample_count
			self.path_component_stats = serialize_component_stats(progress.path_component_stats)
			if progress.status == "error" and progress.message:
				self._append_error(progress.message, progress.current_url)
			if progress.new_emails:
				new_batch = list(progress.new_emails)
				self.raw_emails.update(new_batch)
				valid, invalid = filter_valid_emails(new_batch)
				self.valid_emails.update(valid)
				self.invalid_emails.update(invalid)
				source_url = progress.current_url or self.source_url
				if source_url:
					for email in valid:
						self.email_sources.setdefault(email, set()).add(source_url)
					for email in invalid:
						self.email_sources.setdefault(email, set()).add(source_url)
			if progress.status == "finished":
				self.finished_at = now
			elif progress.status == "cancelled":
				self.status = "cancelled"
				self.finished_at = now
				if progress.message:
					self._append_error(progress.message, progress.current_url)

	def finalize(self, emails: set[str], component_stats: Optional[dict[str, Any]] = None) -> None:
		valid, invalid = filter_valid_emails(emails)
		now = time.time()
		with self._lock:
			self.raw_emails = set(emails)
			self.valid_emails = set(valid)
			self.invalid_emails = set(invalid)
			self.unique_emails = len(emails)
			fallback_source = self.source_url
			if fallback_source:
				for email in self.valid_emails:
					self.email_sources.setdefault(email, set()).add(fallback_source)
				for email in self.invalid_emails:
					self.email_sources.setdefault(email, set()).add(fallback_source)
			if component_stats is not None:
				self.path_component_stats = serialize_component_stats(component_stats)
			if self.status not in {"failed", "cancelled"}:
				self.status = "finished"
			self.finished_at = now
			self.last_update = now

	def mark_failed(self, message: str) -> None:
		now = time.time()
		with self._lock:
			self.status = "failed"
			self._append_error(message, None)
			self.finished_at = now
			self.last_update = now

	def mark_cancelled(self, message: Optional[str] = None) -> None:
		now = time.time()
		with self._lock:
			self.status = "cancelled"
			if message:
				self._append_error(message, None)
			self.finished_at = now
			self.last_update = now

	def request_cancel(self) -> bool:
		now = time.time()
		with self._lock:
			if self.cancel_event.is_set():
				return False
			if self.status in {"finished", "failed", "cancelled"}:
				return False
			self.cancel_event.set()
			if self.status in {"pending"}:
				self.status = "cancelled"
				self.finished_at = now
			else:
				self.status = "cancelling"
			self.last_update = now
			return True

	def snapshot(self) -> dict[str, Any]:
		with self._lock:
			progress_ratio = None
			if self.max_pages:
				progress_ratio = min(1.0, self.processed_count / float(self.max_pages))
			return {
				"id": self.id,
				"status": self.status,
				"source_url": self.source_url,
				"processed_count": self.processed_count,
				"max_pages": self.max_pages,
				"concurrency": self.concurrency,
				"queued_count": self.queued_count,
				"unique_emails": self.unique_emails,
				"current_url": self.current_url,
				"errors": [error.to_dict() for error in self.errors],
				"error_summary": self._build_error_summary_locked(),
				"valid_emails": sorted(self.valid_emails),
				"invalid_emails": sorted(self.invalid_emails),
				"email_sources": {
					email: sorted(sources)
					for email, sources in self.email_sources.items()
				},
				"path_component_stats": {
					component: dict(stats)
					for component, stats in self.path_component_stats.items()
				},
				"started_at": self.started_at,
				"finished_at": self.finished_at,
				"last_update": self.last_update,
				"progress_ratio": progress_ratio,
				"timing": {
					"last_fetch_time": self.last_fetch_time,
					"last_parse_time": self.last_parse_time,
					"avg_fetch_time": self.avg_fetch_time,
					"avg_parse_time": self.avg_parse_time,
					"max_fetch_time": self.max_fetch_time,
					"max_parse_time": self.max_parse_time,
					"min_fetch_time": self.min_fetch_time,
					"min_parse_time": self.min_parse_time,
					"total_fetch_time": self.total_fetch_time,
					"total_parse_time": self.total_parse_time,
					"fetch_sample_count": self.fetch_sample_count,
					"parse_sample_count": self.parse_sample_count,
				},
				"form_values": dict(self.form_values),
				"cancel_requested": self.cancel_event.is_set(),
			}

	def _append_error(self, message: str, url: Optional[str]) -> None:
		if not message:
			return
		self.errors.append(JobError(message=message, url=url))

	def _build_error_summary_locked(self) -> dict[str, Any]:
		total = len(self.errors)
		if total == 0:
			return {
				"total": 0,
				"unique": 0,
				"top": None,
				"by_message": [],
			}
		message_counter = Counter(error.message for error in self.errors)
		by_message = [
			{"message": message, "count": count}
			for message, count in message_counter.most_common()
		]
		top = by_message[0] if by_message else None
		return {
			"total": total,
			"unique": len(message_counter),
			"top": top,
			"by_message": by_message,
		}


jobs: dict[str, ScrapeJob] = {}
jobs_lock = threading.Lock()


def register_job(job: ScrapeJob) -> None:
	with jobs_lock:
		jobs[job.id] = job


def get_job(job_id: str) -> Optional[ScrapeJob]:
	with jobs_lock:
		return jobs.get(job_id)


def execute_job(job: ScrapeJob) -> None:
	try:
		result = scrape_website(
			job.source_url,
			max_count=job.max_pages,
			stay_in_domain=job.stay_in_domain,
			exclude_strs=job.exclude_terms,
			timeout=job.timeout,
			output_file=None,
			progress_callback=job.apply_progress,
			cancellation_event=job.cancel_event,
			concurrency=job.concurrency,
		)
		job.finalize(result.emails, result.path_component_stats)
	except ScrapeCancelled:
		job.mark_cancelled()
	except Exception as exc:  # pragma: no cover - bubbled to UI via job errors
		job.mark_failed(str(exc))
	finally:
		with job._lock:
			job.worker = None


@app.route("/", methods=["GET", "POST"])
def index():
	errors: list[str] = []
	job_state: Optional[dict[str, Any]] = None

	form_defaults = {
		"domain": "",
		"max_pages": 100,
		"allow_external": False,
		"exclude": "",
		"concurrency": 1,
	}

	form_values = dict(form_defaults)

	if request.method == "POST":
		domain = request.form.get("domain", "").strip()
		form_values["domain"] = domain

		max_pages_raw = request.form.get("max_pages", str(form_defaults["max_pages"]))
		try:
			max_pages = max(1, min(int(max_pages_raw), 1000))
		except (TypeError, ValueError):
			max_pages = form_defaults["max_pages"]
			errors.append("Max pages must be a number; falling back to default.")
		form_values["max_pages"] = max_pages

		allow_external = request.form.get("allow_external") == "on"
		form_values["allow_external"] = allow_external

		exclude_terms_raw = request.form.get("exclude", "")
		form_values["exclude"] = exclude_terms_raw
		exclude_terms = parse_exclude_terms(exclude_terms_raw)

		concurrency_raw = request.form.get("concurrency", str(form_defaults["concurrency"]))
		try:
			concurrency = max(1, min(int(concurrency_raw), 16))
		except (TypeError, ValueError):
			concurrency = form_defaults["concurrency"]
			errors.append("Concurrency must be a number; falling back to default.")
		form_values["concurrency"] = concurrency

		if not domain:
			errors.append("Please provide a website domain or URL.")
		else:
			start_url = normalize_url(domain)
			job = ScrapeJob(
				id=uuid.uuid4().hex,
				source_url=start_url,
				max_pages=max_pages,
				stay_in_domain=not allow_external,
				exclude_terms=exclude_terms,
				timeout=10.0,
				concurrency=concurrency,
				form_values=form_values.copy(),
			)
			register_job(job)
			worker = threading.Thread(target=execute_job, args=(job,), daemon=True)
			job.worker = worker
			worker.start()
			return redirect(url_for("index", job_id=job.id))

		return render_template(
			"index.html",
			errors=errors,
			form_values=form_values,
			job=None,
		)

	job_id = request.args.get("job_id")
	if job_id:
		job = get_job(job_id)
		if job:
			job_state = job.snapshot()
			form_values.update(job_state.get("form_values", {}))
		else:
			errors.append("We couldn't find a scraping job with that identifier.")

	return render_template(
		"index.html",
		errors=errors,
		form_values=form_values,
		job=job_state,
	)


@app.route("/api/jobs/<job_id>", methods=["GET"])
def job_status(job_id: str):
	job = get_job(job_id)
	if not job:
		abort(404, description="Job not found")
	return jsonify(job.snapshot())


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id: str):
	job = get_job(job_id)
	if not job:
		abort(404, description="Job not found")
	requested = job.request_cancel()
	status_code = 202 if requested else 200
	return jsonify({
		"id": job.id,
		"status": job.status,
		"cancel_requested": job.cancel_event.is_set(),
	}), status_code


if __name__ == "__main__":
	port = int(os.environ.get("PORT", "5000"))
	app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
