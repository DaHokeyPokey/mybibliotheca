# Changelog

All notable changes to MyBibliotheca will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Admin-only "New update available" notice backed by a cached, silent GitHub releases check (disable with `UPDATE_CHECK_ENABLED=false`).
- PostgreSQL schema version 7 adds the `journal_entries` and `progress_updates` tables; it is applied automatically at startup. Older versions of the app refuse to start on a database that has it, so back up before upgrading if you might need to roll back. Deleting a book also deletes your progress updates for it and keeps your journal entries, unlinked from the book.
- JSON endpoints for per-book reading progress (`/progress/...`) and journal entries (`/journal/...`), on PostgreSQL only. A progress update also feeds that day's reading log and can finish the book. Journal text is sanitized when saved, using the new `nh3` dependency.

### Security
- Settings pages no longer send saved secrets (OpenAI API key, SMTP password, personal Audiobookshelf key, OPDS password) back to the browser. Leaving one of these fields blank keeps the saved value; type a new value to replace it.
- Book, person, category, file and error text shown in search dropdowns, previews, modals and stats tooltips is now HTML-escaped, so a crafted title or name can no longer run script in another user's browser.
- The backup schedule API now requires a CSRF token instead of being exempt.
- The setup confirmation step no longer prints the submitted form, including its CSRF token, to the browser console.
- The first-run setup wizard closes once any account exists; before, anyone could reach its steps and create a new admin account on a running install.
- The raw book data view (`/book/<uid>/raw`) is admin-only and never includes password hashes. Before, any user who had a book could see other users' password hashes, emails and personal notes on installs migrated from Kuzu.
- The post-restore page (`/auth/simple-backup/restore-complete`) now requires an admin. Before, anyone could open it and restart a worker process.
- Deactivating or deleting a user now ends their existing sessions and remember-me logins. Deactivated admins no longer count toward the "last admin" protection, and admins can no longer deactivate their own account.
- Failed logins are counted in one database update, so parallel guesses can no longer slip past the 5-attempt lock on PostgreSQL. After a lock expires, the count starts again instead of re-locking on the next mistake.
- After login, the app only redirects to pages on this site; links such as `/login?next=//other.example` now land on the home page.
- Person website and official links must be `http`/`https`; `javascript:` links are dropped when saved and are no longer shown if they were saved before.
- Downloading a cover from a URL now checks every redirect against private and local addresses, and stops sending credentials when a redirect leaves the original host.

### Changed
- Deleting a custom field is refused while other users still have values for it (on PostgreSQL), and the error message says why; before, it erased their values too.
- The library page on PostgreSQL counts and pages through your own books, not the whole shared catalog.
- Docker: Gunicorn workers are no longer restarted every ~1000 requests; the restart killed long imports mid-file.
- Removed the unused `scrypt` and `cryptography` dependencies.
- On PostgreSQL, deleting a book removes it only from your own library (with your custom fields for it). The book stays for other users who have it, and leaves the catalog when its last owner removes it. Before, deleting a book removed it from every user's library. You can no longer delete a book that is not in your library.

### Fixed
- Scheduled backups and OPDS/Audiobookshelf auto-syncs run once, not once per worker (six nightly backups on the default Docker setup). Backup settings and the backup list now stay in step across workers, and an OPDS auto-sync is no longer queued again while it is still running.
- Cancel now stops a running CSV import, and the job shows as cancelled.
- ISBN-only CSV rows whose metadata lookup was capped or ran out of time are imported and marked Needs Review, as the progress message says, instead of being dropped as errors.
- Community stats, user profiles and the monthly review image show finished and current books again on PostgreSQL.
- Editing a book now reports an error when part of the save fails, instead of "saved" with authors or categories missing.
- The library page no longer shows deleted books, or misses new ones, depending on which worker answers.
- Book lists such as the reading-log book picker load with a fixed number of database queries instead of several per book.
- Deleting a series from its detail page works again (the request was missing its CSRF token).
- Reading Journey stats page: its scripts were broken by stray CSS and never ran; clicks and filters work again.
- Library Network explorer no longer stops setting up when its density slider is absent.
- Adding a book no longer throws a console error after a lookup (call to an undefined cover-picker function removed).
- On the book edit page, pressing Enter to add a new contributor no longer throws an error; the contributor is added and marked as new.
- The `form_start`/`csrf_token` form macros no longer recurse forever; the CSRF macro is now `csrf_field()`, which fixes the CSRF guide page.

### Removed
- Unused templates `edit_book.html`, `add_book_new.html` and `import_books_confirmation.html`, and five unused helper functions.
- Unused modules `app/utils.py` (shadowed by the `app/utils/` package), `app/sqlite_migration_service.py` and `network_data_fix.py`.

## [3.0.1] - 2026-11-02

### Fixed
- Library Journey timeline no longer places books without a finish date into a bogus year (e.g. the import year); only the selected date type is used (#208).
- Editing, deleting and viewing locations no longer fails with a server error on PostgreSQL, and a location saved on the book edit form now shows up when you reopen it, including edits that change nothing else and clearing the location (#255).
- The library page shows book authors again, and sorting by author works (#256).
- First-run setup now shows the real reason when it fails instead of a generic "Setup failed" message (#170).
- Goodreads and CSV imports: merging into an existing book no longer silently drops dates, status, rating and notes; Goodreads ratings and reviews are now imported; failed merges are reported as errors instead of successes; and an undecodable byte or BOM no longer aborts an import at any entry point (upload preview, execute, simple upload, format detection, onboarding import); a warning reports how many characters were replaced (#183, #216).
- Simple upload no longer reports "Import started" when the file's format is unrecognized and every row would be skipped; it returns a clear error instead.
- The AI settings page error (`load_ai_config` NameError) was fixed in 2.2.2 and is included (#251).

### Security
- Passwords, password hashes, CSRF tokens and other secrets are no longer written to logs (onboarding/session debug output, user creation); a global log filter masks them as a safeguard.

### Added
- "New update available" notice for administrators that checks GitHub releases and links to the release page. Cached, non-blocking, sends no identifying data, and can be disabled with `UPDATE_CHECK_ENABLED=false`.

### Hotfix
- Fixed an empty glass card appearing under the navbar for administrators: the hidden update notice was wrapped in a styled container. Republished as the same 3.0.1 tags.

### Changed
- The page footer now shows the installed version read from the application instead of a hard-coded number.
- README documentation links now point to docs.mybibliotheca.org.

## [3.0.0] - 2026-10-20

### Changed
- PostgreSQL is now the primary database for new installations.
- Added a verified in-app migration wizard for existing Kuzu libraries; upgrading the application alone does not migrate data.
- Updated installation, configuration, and migration documentation for the PostgreSQL deployment.

### Migration notes
- Back up the application data directory and PostgreSQL storage before upgrading.
- Existing Kuzu installations remain on Kuzu until an administrator completes the migration wizard.
- Keep the Kuzu database and verified backups until the PostgreSQL library is checked.

## [2.2.2] - 2026-10-19

### Fixed
- Restored the self-hosted Quagga2 camera scanner bundle and removed duplicate camera stream acquisition.
- Restricted CSV import processing to the current user's temporary upload files.
- Fixed the legacy CSV upload route to process imported books instead of reporting a false success.
- Fixed background import error handlers to use captured user IDs outside the request context.
- Restored missing server settings panel loader imports.

### Changed
- Updated Python dependencies and lockfile.

## [2.2.1] - 2026-09-09

### Added
- Added Google Books API support for ISBN metadata lookups and imports.
- Re-enabled camera-based ISBN barcode scanning in Fast Add mode.

### Fixed
- Improved ISBN validation and scanner feedback, with manual and USB/Bluetooth scanner fallbacks.
- Improved CSV import metadata enrichment, timeout handling, and partial-result reporting.
- Fixed static asset URL generation on Settings and book-detail pages.


## [2.2.0] - 2026-08-28

### Added
- Added title-based external book search alongside ISBN lookup when adding books.
- Added support for selecting search results that do not include an ISBN.

### Fixed
- Fixed author card links using an incorrect route on the People page.
- Fixed add-book modal backdrops blocking modal interaction.
- Fixed case-sensitive username login and profile validation.
- Fixed mobile layouts across the library, add-book, and book-detail pages.
- Fixed book-detail covers overflowing their column on mobile devices.

### Improved
- Refreshed project dependencies and lockfile.
- Improved mobile navigation, page controls, pagination, and book-card sizing.
- Removed the decorative bubble from the application header.

## [2.0.1] - 2025-11-13

### Fixed
- Fixed admin blueprint error preventing admins from creating new users without internal server errors ([#110](https://github.com/pickles4evaaaa/mybibliotheca/pull/110))
- Fixed user creation issues and backup settings persistence ([#109](https://github.com/pickles4evaaaa/mybibliotheca/pull/109))
- Fixed security alert by removing user_data dict logging that contained sensitive fields
- Fixed backup settings capture for unchecked state
- Fixed stats mobile display issues
- Fixed dark mode text visibility across UI components ([#104](https://github.com/pickles4evaaaa/mybibliotheca/pull/104))
- Fixed duplicate book detection by removing overly aggressive fuzzy title matching that caused false positives ([#98](https://github.com/pickles4evaaaa/mybibliotheca/pull/98))
- Fixed worker timeout in Goodreads import by adding metadata fetch timeouts ([#99](https://github.com/pickles4evaaaa/mybibliotheca/pull/99))
- Fixed blurry book covers in view book page
- Fixed `@app.before_first_request` decorator issue (removed in Flask 2.2+) that was causing application crashes on startup

### Improved
- Updated Python packages to latest stable versions ([#102](https://github.com/pickles4evaaaa/mybibliotheca/pull/102))
- Enhanced reading journey calendar view with larger, more appealing book covers
- Added pagination controls at bottom of library view for better navigation ([#97](https://github.com/pickles4evaaaa/mybibliotheca/pull/97))
- Simplified header/footer UI and improved mobile layout ([#92](https://github.com/pickles4evaaaa/mybibliotheca/pull/92))
- Updated README with quick start guide and removed outdated setup instructions
- Added comprehensive dark mode CSS overrides for Bootstrap utility classes
- Updated version tag from Alpha 2.0.0 to v2.0.0-beta.1 and then to v2.0.1

### Added
- Added GitHub Copilot instructions and automated setup steps ([#106](https://github.com/pickles4evaaaa/mybibliotheca/pull/106))
- Added wheel files to .gitignore for cleaner repository

### Documentation
- Updated README to version 2.0.1+ with enhanced warning message
- Added feature screenshots showcasing library homepage, reading log, and book details
- Improved documentation for beta deployment

---

## [2.0.0] - 2025-11-12

### Major Changes
- **Complete Database Overhaul**: Migrated from traditional SQL to KuzuDB graph database
  - Advanced relationship modeling for better recommendations
  - Complex queries that were previously impossible
  - Foundation for future social features
  - Better data persistence and integrity

### Added
- **Multi-User Support from Day One**
  - Secure authentication with bcrypt password hashing
  - Complete user data isolation
  - Admin tools for user management
  - No default credentials—full user control
- ISBN book lookup with automatic metadata fetching
- Bulk import from Goodreads CSV
- Detailed reading logs with notes and progress tracking
- Beautiful, responsive UI that works on any device
- Reading stats and streaks to keep users motivated

### Notes
- Version 1.1.1 is deprecated and that branch will no longer be maintained
- For installation documentation, visit: https://mybibliotheca.org/

---

## Earlier Versions

For changes in versions prior to 2.0.0, please refer to the [GitHub Releases](https://github.com/pickles4evaaaa/mybibliotheca/releases) page.
