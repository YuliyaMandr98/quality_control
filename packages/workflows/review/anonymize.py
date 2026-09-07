#!/usr/bin/env python3
"""
Anonymization and Redaction Script for Trace2Quality

Scrubs sensitive data (JWTs, emails, phone numbers, credit card numbers,
system/git usernames, and internal/local URLs/IPs) before sending to LLMs.
Supports stdin/stdout, files, and macOS clipboard (pbcopy/pbpaste).
"""

import argparse
import getpass
import ipaddress
import json
import os
import re
import sys
import subprocess
from typing import Any, Callable, Dict, List, Set, Tuple
from urllib.parse import urlparse

# Regular Expressions for Detection

# JWT: matches standard 3-part Base64 URL encoded token
JWT_REGEX = re.compile(
    r'\beyJ[A-Za-z0-9-_]+\.eyJ[A-Za-z0-9-_]+\.[A-Za-z0-9-_=]*\b'
)

# Email: standard email format RFC 5322
EMAIL_REGEX = re.compile(
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
)

# Credit Cards: 13-19 digits, allowing optional spaces/hyphens
# (Luhn check is run against candidates matching this regex)
CARD_REGEX = re.compile(
    r'\b(?:\d[ -\xa0]*?){13,19}\b'
)

# Phone Numbers: US/Russian/International formats
PHONE_REGEX = re.compile(
    # Russian/International format (e.g. +7 999 123-4567, 8 (999) 123-45-67)
    r'(?<!\w)(?:\+?7|8)[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{2}[-.\s]?\d{2}\b'
    r'|'
    # US format (e.g. +1-202-555-0143, (202) 555-0143)
    r'(?<!\w)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'
    r'|'
    # General International format (with plus and spaces/dashes)
    r'(?<!\w)\+?\d{1,4}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b'
)

# URL: matches http, https, ftp links
URL_REGEX = re.compile(
    r'\b(?:https?|ftp)://[A-Za-z0-9-._~:/?#\[\]@!$&\'()*+,;=]+\b'
)

# IPv4 / IPv6
IP_REGEX = re.compile(
    r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b'
    r'|'
    r'\b(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}\b'
)

# Heuristic capitalized name pattern (2 or 3 capitalized words, e.g. John Doe, Иван Иванович Петров)
NAME_HEURISTIC_REGEX = re.compile(
    r'\b(?:[A-ZА-ЯЁ][a-zа-яё]+)(?:\s+[A-ZА-ЯЁ][a-zа-яё]+){1,2}\b'
)

# Common words to ignore when doing heuristic name matching
STOP_WORDS = {
    # English days, months, common tech/business terms
    "the", "this", "that", "these", "those", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "january", "february", "march",
    "april", "may", "june", "july", "august", "september", "october", "november",
    "december", "error", "warning", "info", "debug", "trace", "exception", "failed",
    "success", "status", "server", "client", "project", "database", "query", "user",
    "admin", "system", "file", "path", "directory", "folder", "config", "setting",
    "version", "build", "release", "test", "demo", "run", "process", "thread",
    "application", "service", "api", "url", "http", "https", "token", "header",
    "response", "request", "data", "metadata", "value", "key", "id", "uuid", "guid",
    "first", "last", "name", "type", "code", "mode", "host", "port", "main", "root",
    # Russian days, months, common tech/business terms
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
    "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь",
    "октябрь", "ноябрь", "декабрь", "ошибка", "предупреждение", "информация", "статус",
    "сервер", "клиент", "проект", "база", "запрос", "пользователь", "админ", "система",
    "файл", "путь", "папка", "каталог", "версия", "сборка", "релиз", "тест", "демо",
    "запуск", "процесс", "поток", "приложение", "сервис", "данные", "значение", "ключ",
    "пожалуйста", "внимание", "карта", "телефон", "адрес", "почта", "письмо", "сообщение",
    "первый", "последний", "имя", "тип", "код", "режим", "хост", "порт", "главный"
}



class RedactionStats:
    """Tracks statistics of redacted elements"""
    def __init__(self) -> None:
        self.stats: Dict[str, int] = {
            "jwts": 0,
            "emails": 0,
            "cards": 0,
            "phones": 0,
            "usernames": 0,
            "paths": 0,
            "internal_urls": 0,
            "custom_words": 0
        }

    def increment(self, category: str, count: int = 1) -> None:
        if category in self.stats:
            self.stats[category] += count

    def get_summary(self) -> str:
        lines = ["Redaction Summary:"]
        for key, value in self.stats.items():
            if value > 0:
                lines.append(f"  - {key.replace('_', ' ').capitalize()}: {value}")
        if len(lines) == 1:
            return "No sensitive data detected/redacted."
        return "\n".join(lines)


def is_luhn_valid(card_number: str) -> bool:
    """Applies the Luhn algorithm to check if a sequence of digits is a valid credit card."""
    digits = [int(d) for d in card_number if d.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    reverse_digits = digits[::-1]
    for i, digit in enumerate(reverse_digits):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def is_private_ip(ip_str: str) -> bool:
    """Returns True if the IP address is private/loopback/link-local."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def is_phone_number(match_str: str) -> bool:
    """Verifies that a matched phone number is not a version number, date, or other ID."""
    digits_only = "".join(c for c in match_str if c.isdigit())
    if len(digits_only) < 7 or len(digits_only) > 15:
        return False
    
    # Exclude YYYY-MM-DD dates
    if re.match(r'^\d{4}-\d{2}-\d{2}$', match_str):
        return False
    
    # Exclude pure short numbers
    if re.match(r'^\d+$', match_str) and len(digits_only) < 10:
        return False
    
    # Exclude version patterns (e.g. 1.2.3.4)
    if re.match(r'^\d+\.\d+\.\d+\.\d+$', match_str):
        return False
        
    return True


def get_git_info() -> Tuple[str, str]:
    """Retrieves username and email from git config."""
    try:
        username = subprocess.check_output(
            ["git", "config", "user.name"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        username = ""
    try:
        email = subprocess.check_output(
            ["git", "config", "user.email"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        email = ""
    return username, email


def get_system_username() -> str:
    """Returns the current system username."""
    try:
        return getpass.getuser()
    except Exception:
        return ""


class Redactor:
    def __init__(
        self,
        custom_names: List[str] = None,
        internal_domains: List[str] = None,
        auto_git: bool = True,
        auto_names: bool = False
    ) -> None:
        self.stats = RedactionStats()
        self.custom_names = [n.strip() for n in (custom_names or []) if n.strip()]
        self.internal_domains = [d.strip().lower() for d in (internal_domains or []) if d.strip()]
        self.auto_names = auto_names
        
        # Mappings for consistent replacement
        self.email_map: Dict[str, str] = {}
        self.phone_map: Dict[str, str] = {}
        self.url_map: Dict[str, str] = {}
        
        # Collect system information
        self.sys_username = get_system_username()
        self.git_name = ""
        self.git_email = ""
        
        if auto_git:
            self.git_name, self.git_email = get_git_info()
            if self.git_name and self.git_name not in self.custom_names:
                self.custom_names.append(self.git_name)
            if self.git_email and self.git_email not in self.custom_names:
                self.custom_names.append(self.git_email)

    def redact_text(self, text: str) -> str:
        """Main entry point to anonymize text."""
        # 1. JWT removal
        text = self._redact_jwts(text)
        
        # 2. Credit card masking
        text = self._redact_cards(text)
        
        # 3. Emails replacement
        text = self._redact_emails(text)
        
        # 4. Phone numbers hiding
        text = self._redact_phones(text)
        
        # 5. Path masking for system usernames
        text = self._redact_paths(text)
        
        # 6. Internal URLs and IPs masking
        text = self._redact_urls_and_ips(text)
        
        # 7. Custom names / usernames / git name removal
        text = self._redact_custom_words(text)
        
        # 8. Heuristic capitalized name auto-redaction (optional)
        if self.auto_names:
            text = self._redact_heuristic_names(text)
            
        return text

    def _redact_heuristic_names(self, text: str) -> str:
        def name_repl(match: re.Match) -> str:
            full_match = match.group(0)
            words = full_match.split()
            # If any of the words is in the stop words list, do not redact
            for word in words:
                if word.lower() in STOP_WORDS:
                    return full_match
            
            # Avoid all-caps phrases (e.g. "ERROR STATUS")
            if all(w.isupper() for w in words):
                return full_match
                
            self.stats.increment("usernames")
            return "[USER_NAME]"
            
        return NAME_HEURISTIC_REGEX.sub(name_repl, text)


    def _redact_jwts(self, text: str) -> str:
        def jwt_repl(match: re.Match) -> str:
            self.stats.increment("jwts")
            return "[JWT_REMOVED]"
        return JWT_REGEX.sub(jwt_repl, text)

    def _redact_cards(self, text: str) -> str:
        def card_repl(match: re.Match) -> str:
            raw_val = match.group(0)
            digits = "".join(c for c in raw_val if c.isdigit())
            if is_luhn_valid(digits):
                self.stats.increment("cards")
                # Keep first 4 and last 4, mask middle
                first_4 = digits[:4]
                last_4 = digits[-4:]
                return f"{first_4}-XXXX-XXXX-{last_4}"
            return raw_val
        return CARD_REGEX.sub(card_repl, text)

    def _redact_emails(self, text: str) -> str:
        def email_repl(match: re.Match) -> str:
            email = match.group(0)
            email_lower = email.lower()
            if email_lower not in self.email_map:
                idx = len(self.email_map) + 1
                self.email_map[email_lower] = f"user{idx}@example.com"
                self.stats.increment("emails")
            return self.email_map[email_lower]
        return EMAIL_REGEX.sub(email_repl, text)

    def _redact_phones(self, text: str) -> str:
        def phone_repl(match: re.Match) -> str:
            phone = match.group(0)
            if not is_phone_number(phone):
                return phone
            if phone not in self.phone_map:
                idx = len(self.phone_map) + 1
                self.phone_map[phone] = f"[PHONE_{idx}]"
                self.stats.increment("phones")
            return self.phone_map[phone]
        return PHONE_REGEX.sub(phone_repl, text)

    def _redact_paths(self, text: str) -> str:
        # Replaces /Users/username/ or C:\Users\username\ or /home/username/
        if not self.sys_username or len(self.sys_username) < 3:
            return text
        
        # Match standard user folder paths
        pattern_str = rf"(/Users/|/home/|[a-zA-Z]:\\Users\\|\\home\\){re.escape(self.sys_username)}"
        pattern = re.compile(pattern_str, re.IGNORECASE)
        
        def path_repl(match: re.Match) -> str:
            self.stats.increment("paths")
            return f"{match.group(1)}user_redacted"
            
        return pattern.sub(path_repl, text)

    def _redact_urls_and_ips(self, text: str) -> str:
        # First redact URLs
        def url_repl(match: re.Match) -> str:
            url = match.group(0)
            try:
                parsed = urlparse(url)
                hostname = parsed.hostname
                if not hostname:
                    return url
                
                is_internal = False
                hostname_lower = hostname.lower()
                
                # Check internal rules
                if hostname_lower == "localhost" or hostname_lower.endswith(
                    (".local", ".internal", ".lan", ".localdomain")
                ):
                    is_internal = True
                elif is_private_ip(hostname):
                    is_internal = True
                elif self.internal_domains:
                    for d in self.internal_domains:
                        if hostname_lower == d or hostname_lower.endswith("." + d):
                            is_internal = True
                            break
                            
                if is_internal:
                    if url not in self.url_map:
                        idx = len(self.url_map) + 1
                        self.url_map[url] = f"[INTERNAL_URL_{idx}]"
                        self.stats.increment("internal_urls")
                    return self.url_map[url]
            except Exception:
                pass
            return url

        text = URL_REGEX.sub(url_repl, text)

        # Redact raw IP addresses if they are private
        def ip_repl(match: re.Match) -> str:
            ip = match.group(0)
            if is_private_ip(ip):
                self.stats.increment("internal_urls")
                return "[INTERNAL_IP]"
            return ip

        text = IP_REGEX.sub(ip_repl, text)
        return text

    def _redact_custom_words(self, text: str) -> str:
        # Add system username if it's longer than 3 chars
        words_to_redact = set()
        if self.sys_username and len(self.sys_username) >= 3:
            words_to_redact.add(self.sys_username)
            
        for name in self.custom_names:
            if name and len(name) >= 3:
                words_to_redact.add(name)
                # If name has spaces (e.g. "John Doe"), add parts
                if " " in name:
                    for part in name.split():
                        if len(part) >= 3:
                            words_to_redact.add(part)

        if not words_to_redact:
            return text

        # Sort by length descending to replace longer words first (avoid partial matches)
        sorted_words = sorted(list(words_to_redact), key=len, reverse=True)
        
        # Build regex matching any of these words on word boundaries or special username characters
        pattern_parts = []
        for word in sorted_words:
            # Escape for safety, and use boundary
            escaped = re.escape(word)
            pattern_parts.append(rf"\b{escaped}\b")
            
        # Combine
        combined_regex = re.compile("|".join(pattern_parts), re.IGNORECASE)
        
        def word_repl(match: re.Match) -> str:
            matched_word = match.group(0)
            self.stats.increment("usernames")
            return "[USER_NAME]"

        return combined_regex.sub(word_repl, text)


def clipboard_get() -> str:
    """Gets text from the macOS clipboard using pbpaste."""
    try:
        return subprocess.check_output(["pbpaste"], text=True)
    except Exception:
        return ""


def clipboard_set(text: str) -> None:
    """Sets text to the macOS clipboard using pbcopy."""
    try:
        process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
        process.communicate(text)
    except Exception as e:
        print(f"Error copying to clipboard: {e}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrub and anonymize logs/files before sharing with public LLMs."
    )
    parser.add_argument(
        "-i", "--input",
        help="Path to the input file (reads from stdin if not specified)."
    )
    parser.add_argument(
        "-o", "--output",
        help="Path to the output file (writes to stdout if not specified)."
    )
    parser.add_argument(
        "-c", "--clipboard",
        action="store_true",
        help="Read from macOS clipboard, anonymize, and copy back to clipboard."
    )
    parser.add_argument(
        "--domains",
        help="Comma-separated list of internal corporate domains to mask."
    )
    parser.add_argument(
        "--names",
        help="Comma-separated list of custom names/usernames to redact."
    )
    parser.add_argument(
        "--names-file",
        help="Path to a text file containing names/companies to redact (one per line)."
    )
    parser.add_argument(
        "--auto-names",
        action="store_true",
        help="Automatically detect and redact capitalized names (e.g. 'John Smith') via heuristics."
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="Do not automatically retrieve and redact user name/email from git config."
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print summary of redacted data to stderr."
    )

    args = parser.parse_args()

    custom_domains = []
    if args.domains:
        custom_domains = args.domains.split(",")

    custom_names = []
    if args.names:
        custom_names = args.names.split(",")

    if args.names_file:
        try:
            with open(args.names_file, "r", encoding="utf-8") as f:
                for line in f:
                    name = line.strip()
                    if name and not name.startswith("#"):
                        custom_names.append(name)
        except Exception as e:
            print(f"Error reading names file {args.names_file}: {e}", file=sys.stderr)
            sys.exit(1)

    redactor = Redactor(
        custom_names=custom_names,
        internal_domains=custom_domains,
        auto_git=not args.no_git,
        auto_names=args.auto_names
    )

    # 1. Read input
    if args.clipboard:
        if sys.platform != "darwin":
            print("Clipboard mode is currently only supported on macOS.", file=sys.stderr)
            sys.exit(1)
        input_text = clipboard_get()
        if not input_text:
            print("Clipboard is empty.", file=sys.stderr)
            sys.exit(0)
    elif args.input:
        try:
            with open(args.input, "r", encoding="utf-8") as f:
                input_text = f.read()
        except Exception as e:
            print(f"Error reading file {args.input}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Read from stdin
        if sys.stdin.isatty():
            print("Waiting for input from stdin (Press Ctrl+D to finish, or use --help for info)...", file=sys.stderr)
        input_text = sys.stdin.read()

    # 2. Process
    output_text = redactor.redact_text(input_text)

    # 3. Write output
    if args.clipboard:
        clipboard_set(output_text)
        print("Anonymized content successfully copied back to macOS clipboard!", file=sys.stderr)
    elif args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output_text)
            print(f"Anonymized content written to {args.output}", file=sys.stderr)
        except Exception as e:
            print(f"Error writing to file {args.output}: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Write to stdout
        sys.stdout.write(output_text)
        # Add trailing newline if stdin didn't have one and we are on a tty
        if sys.stdout.isatty() and not output_text.endswith("\n"):
            sys.stdout.write("\n")

    # 4. Stats
    if args.stats:
        print("\n" + redactor.stats.get_summary(), file=sys.stderr)


if __name__ == "__main__":
    main()
