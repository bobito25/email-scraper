from collections.abc import Iterable
from typing import Set, Tuple

from email_validator import validate_email, EmailNotValidError


def filter_valid_emails(emails: Iterable[str]) -> Tuple[Set[str], Set[str]]:
    """Validate and normalize raw email strings, returning valid and invalid sets."""

    valid_emails: Set[str] = set()
    invalid_emails: Set[str] = set()

    for email in emails:
        email = email.strip()
        if not email:
            continue
        try:
            normalized = validate_email(email)
            valid_emails.add(normalized.email)
        except EmailNotValidError:
            invalid_emails.add(email)

    return valid_emails, invalid_emails


def clean_emails(input_file, output_file):
    with open(input_file, 'r') as infile:
        emails = infile.readlines()

    cleaned_emails, _ = filter_valid_emails(emails)

    with open(output_file, 'w') as outfile:
        for email in sorted(cleaned_emails):
            outfile.write(email + '\n')


if __name__ == "__main__":
    clean_emails('emails.txt', 'cleaned_emails.txt')